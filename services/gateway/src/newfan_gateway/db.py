# mypy: ignore-errors
"""PostgreSQL リポジトリ（本番 DB 接続 + RLS, §7 / §11）。

runtime extra（sqlalchemy）が必要。CI では未実行（DB 前提）。テーブルの正本は Alembic
マイグレーション（§15）。本モジュールの ORM モデルは gateway が参照する列のみをミラーする。

テナント分離: リクエスト毎に `SET LOCAL app.tenant_id` を発行し RLS を効かせる（§7.3）。
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Iterator, Optional

from sqlalchemy import JSON, Integer, String, Text, create_engine, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from newfan_gateway.ids import new_id
from newfan_gateway.records import (
    CorrectionRecord,
    DocumentRecord,
    JobRecord,
    PageRecord,
    RunRecord,
    WorkflowRecord,
)
from newfan_schemas import ExtractedField, TableResult


class Base(DeclarativeBase):
    pass


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String, nullable=False)
    storage_uri: Mapped[str] = mapped_column(Text, nullable=False)
    original_name: Mapped[Optional[str]] = mapped_column(Text)
    mime_type: Mapped[str] = mapped_column(Text, nullable=False)
    page_count: Mapped[Optional[int]] = mapped_column(Integer)
    doc_type: Mapped[Optional[str]] = mapped_column(Text)
    external_ref: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String, default="uploaded")


class Page(Base):
    __tablename__ = "pages"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String, nullable=False)
    document_id: Mapped[str] = mapped_column(String, nullable=False)
    page_no: Mapped[int] = mapped_column(Integer, nullable=False)
    width: Mapped[Optional[int]] = mapped_column(Integer)
    height: Mapped[Optional[int]] = mapped_column(Integer)
    image_uri: Mapped[str] = mapped_column(Text, nullable=False)
    preproc: Mapped[dict] = mapped_column(JSON, default=dict)


class ExtractionRun(Base):
    __tablename__ = "extraction_runs"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String, nullable=False)
    document_id: Mapped[str] = mapped_column(String, nullable=False)
    schema_id: Mapped[Optional[str]] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="processing")
    engine_versions: Mapped[dict] = mapped_column(JSON, default=dict)
    options: Mapped[dict] = mapped_column(JSON, default=dict)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    result_version: Mapped[int] = mapped_column(Integer, default=1)
    # fields/tables/review_summary は正規化テーブル（extraction_fields/_tables）が正本。
    # _synced() がそこから RunRecord を組む（§7 の実スキーマに整合）。


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    ref_id: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, default="queued")
    error_code: Mapped[Optional[str]] = mapped_column(String)


class CorrectionLog(Base):
    """§7 の correction_logs。列は DDL と一致させること。

    以前は DDL に無い note を持ち、逆に学習ループが使う doc_type/supplier_key/context を
    欠いていたため、修正の保存が UndefinedColumn で 500 になっていた（実 AWS で検出）。
    doc_type/supplier_key は memory の検索キー、context は embedding の入力（DD-06/DD-07）で、
    idx_corrections_pattern も (tenant_id, doc_type, field_name) 前提。
    """

    __tablename__ = "correction_logs"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String, nullable=False)
    document_id: Mapped[str] = mapped_column(String, nullable=False)
    run_id: Mapped[str] = mapped_column(String, nullable=False)
    field_name: Mapped[str] = mapped_column(String, nullable=False)
    original_value: Mapped[Optional[str]] = mapped_column(Text)
    corrected_value: Mapped[str] = mapped_column(Text, nullable=False)
    doc_type: Mapped[Optional[str]] = mapped_column(Text)
    supplier_key: Mapped[Optional[str]] = mapped_column(Text)
    context: Mapped[Optional[str]] = mapped_column(Text)
    reviewer_id: Mapped[Optional[str]] = mapped_column(Text)


class PgRepository:
    """Repository の PostgreSQL 実装。"""

    def __init__(self, dsn: str) -> None:
        self._engine = create_engine(dsn, future=True)
        self._session = sessionmaker(self._engine, expire_on_commit=False)

    @contextmanager
    def _rls(self, tenant_id: str) -> Iterator[Session]:
        with self._session() as s:
            # RLS: 当該トランザクションの範囲でテナントを固定（§7.3）
            # SET はバインドパラメータ不可のため set_config(..., is_local=true) を使う
            s.execute(text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": tenant_id})
            yield s
            s.commit()

    def create_document(self, doc: DocumentRecord, pages: list[PageRecord]) -> None:
        with self._rls(doc.tenant_id) as s:
            s.add(Document(**doc.model_dump(exclude={"created_at"})))
            for p in pages:
                s.add(
                    Page(
                        id=f"{doc.id}:{p.page_no}",
                        tenant_id=doc.tenant_id,
                        document_id=doc.id,
                        page_no=p.page_no,
                        width=p.width,
                        height=p.height,
                        image_uri=p.image_uri,
                        preproc=p.preproc,
                    )
                )

    def get_document(self, tenant_id: str, document_id: str) -> Optional[DocumentRecord]:
        with self._rls(tenant_id) as s:
            row = s.get(Document, document_id)
            return _doc_record(row) if row else None

    def list_documents(self, tenant_id, *, status, cursor, limit):
        with self._rls(tenant_id) as s:
            stmt = select(Document).order_by(Document.id.desc()).limit(limit + 1)
            if status:
                stmt = stmt.where(Document.status == status)
            rows = list(s.scalars(stmt))
            has_more = len(rows) > limit
            rows = rows[:limit]
            return [_doc_record(r) for r in rows], (rows[-1].id if has_more else None)

    def get_pages(self, tenant_id, document_id):
        with self._rls(tenant_id) as s:
            rows = s.scalars(select(Page).where(Page.document_id == document_id))
            return [
                PageRecord(
                    page_no=r.page_no,
                    width=r.width,
                    height=r.height,
                    image_uri=r.image_uri,
                    preproc=r.preproc,
                )
                for r in rows
            ]

    def has_active_run(self, tenant_id, document_id):
        with self._rls(tenant_id) as s:
            stmt = select(ExtractionRun).where(
                ExtractionRun.document_id == document_id,
                ExtractionRun.status.in_(("processing", "needs_review")),
            )
            return s.scalars(stmt).first() is not None

    def has_processing_run(self, tenant_id, document_id):
        # has_active_run は needs_review も含むが、こちらは「今まさに処理中」だけ。
        # チャット再抽出（§4.5）は needs_review を取り直す用途が主のため区別する。
        with self._rls(tenant_id) as s:
            stmt = select(ExtractionRun).where(
                ExtractionRun.document_id == document_id,
                ExtractionRun.status == "processing",
            )
            return s.scalars(stmt).first() is not None

    def create_run(self, run: RunRecord) -> None:
        # gateway は抽出前に run 行のみ作成する。fields/tables は worker が
        # extraction_fields/_tables へ書く（§4.3 finalize）。
        with self._rls(run.tenant_id) as s:
            s.add(
                ExtractionRun(
                    id=run.id,
                    tenant_id=run.tenant_id,
                    document_id=run.document_id,
                    schema_id=run.schema_id,
                    status=run.status,
                    engine_versions=run.engine_versions,
                    options=run.options,
                    result_version=run.result_version,
                )
            )

    def get_run(self, tenant_id, run_id):
        with self._rls(tenant_id) as s:
            row = s.get(ExtractionRun, run_id)
            return self._synced(s, row) if row else None

    def get_latest_run(self, tenant_id, document_id):
        with self._rls(tenant_id) as s:
            stmt = (
                select(ExtractionRun)
                .where(ExtractionRun.document_id == document_id)
                .order_by(ExtractionRun.id.desc())
                .limit(1)
            )
            row = s.scalars(stmt).first()
            return self._synced(s, row) if row else None

    def _schema_labels(self, s, schema_id):
        if not schema_id:
            return {}
        r = s.execute(text("SELECT fields FROM field_schemas WHERE id=:i"), {"i": schema_id}).first()
        return {f.get("name"): f.get("label") for f in (r.fields or [])} if r else {}

    def _synced(self, s, row):
        """正規化テーブル（worker 書込の extraction_fields/_tables）を優先して RunRecord を組む。

        gateway 作成直後（抽出前）で正規化行が無ければ非正規化 fields_json にフォールバック。
        label は field_schemas から補完、review_summary は field の review_status から算出する。
        """
        frows = s.execute(
            text(
                "SELECT field_name, value_raw, value_normalized, final_value, confidence, "
                " grounding_score, page_no, bbox, source_quote, span_ids, correction, "
                " validation, review_status FROM extraction_fields WHERE run_id=:r ORDER BY field_name"
            ),
            {"r": row.id},
        ).all()
        if not frows:
            return _run_record(row)  # 非正規化フォールバック
        labels = self._schema_labels(s, row.schema_id)
        fields = [
            ExtractedField.model_validate(
                {
                    "name": r.field_name,
                    "label": labels.get(r.field_name),
                    "value_raw": r.value_raw,
                    "value_normalized": r.final_value if r.final_value is not None else r.value_normalized,
                    "span_ids": r.span_ids or [],
                    "page": r.page_no,
                    "bbox": r.bbox,
                    "source_quote": r.source_quote,
                    "confidence": r.confidence,
                    "grounding_score": r.grounding_score,
                    "correction": r.correction,
                    "validation": r.validation,
                    "review_status": r.review_status,
                }
            )
            for r in frows
        ]
        trows = s.execute(
            text(
                "SELECT name, page_no, structure_html, rows, confidence "
                "FROM extraction_tables WHERE run_id=:r ORDER BY name"
            ),
            {"r": row.id},
        ).all()
        tables = [
            TableResult.model_validate(
                {"name": t.name, "page": t.page_no, "structure_html": t.structure_html, "rows": t.rows or [], "confidence": t.confidence}
            )
            for t in trows
        ]
        counts: dict[str, int] = {}
        for r in frows:
            counts[r.review_status] = counts.get(r.review_status, 0) + 1
        review_summary = {
            "pending": counts.get("pending", 0),
            "auto": counts.get("auto", 0) + counts.get("approved", 0) + counts.get("corrected", 0),
        }
        return RunRecord(
            id=row.id, tenant_id=row.tenant_id, document_id=row.document_id, schema_id=row.schema_id,
            status=row.status, engine_versions=row.engine_versions, options=row.options,
            result_version=row.result_version, fields=fields, tables=tables, review_summary=review_summary,
            fallback_pages=(row.metrics or {}).get("fallback_pages", []),
        )

    def set_document_status(self, tenant_id, document_id, status):
        with self._rls(tenant_id) as s:
            row = s.get(Document, document_id)
            if row:
                row.status = status

    def create_job(self, job: JobRecord) -> None:
        with self._rls(job.tenant_id) as s:
            s.add(Job(id=job.id, tenant_id=job.tenant_id, kind=job.kind, ref_id=job.ref_id, status=job.status))

    def get_job(self, tenant_id, job_id):
        with self._rls(tenant_id) as s:
            row = s.get(Job, job_id)
            if not row:
                return None
            return JobRecord(
                id=row.id, tenant_id=row.tenant_id, kind=row.kind, ref_id=row.ref_id,
                status=row.status, error_code=row.error_code,
            )

    def add_corrections(self, corrections: list[CorrectionRecord]) -> None:
        if not corrections:
            return
        with self._rls(corrections[0].tenant_id) as s:
            for c in corrections:
                s.add(
                    CorrectionLog(
                        id=c.id, tenant_id=c.tenant_id, document_id=c.document_id,
                        run_id=c.run_id, field_name=c.field_name,
                        original_value=c.original_value, corrected_value=c.corrected_value,
                        # 学習ループ（DD-06/DD-07）の検索キーと embedding 入力
                        doc_type=c.doc_type, supplier_key=c.supplier_key,
                        context=c.context, reviewer_id=c.reviewer_id,
                    )
                )

    def list_corrections(self, tenant_id: str, run_id: str) -> list[CorrectionRecord]:
        with self._rls(tenant_id) as s:
            stmt = (
                select(CorrectionLog)
                .where(CorrectionLog.run_id == run_id)
                .order_by(CorrectionLog.id)
            )
            return [
                CorrectionRecord(
                    id=r.id, tenant_id=r.tenant_id, document_id=r.document_id, run_id=r.run_id,
                    field_name=r.field_name, original_value=r.original_value,
                    corrected_value=r.corrected_value, doc_type=r.doc_type,
                    supplier_key=r.supplier_key, context=r.context, reviewer_id=r.reviewer_id,
                )
                for r in s.scalars(stmt)
            ]

    def list_review_runs(self, tenant_id):
        with self._rls(tenant_id) as s:
            stmt = select(ExtractionRun).where(ExtractionRun.status == "needs_review")
            return [self._synced(s, r) for r in s.scalars(stmt)]


def _doc_record(row: Document) -> DocumentRecord:
    return DocumentRecord(
        id=row.id, tenant_id=row.tenant_id, storage_uri=row.storage_uri,
        original_name=row.original_name, mime_type=row.mime_type, page_count=row.page_count,
        doc_type=row.doc_type, external_ref=row.external_ref, status=row.status,
    )


def _run_record(row: ExtractionRun) -> RunRecord:
    # 抽出前（extraction_fields 未書込）の run。fields/tables は空。
    return RunRecord(
        id=row.id, tenant_id=row.tenant_id, document_id=row.document_id, schema_id=row.schema_id,
        status=row.status, engine_versions=row.engine_versions, options=row.options,
        result_version=row.result_version, fields=[], tables=[], review_summary={},
        fallback_pages=(row.metrics or {}).get("fallback_pages", []),
    )


# ============ 管理画面（SCR-04/05/06）Pg 実装 ============


class PgAdminRepository:
    """AdminRepository の PostgreSQL 実装（field_schemas / tenant_rules / 集計）。"""

    def __init__(self, dsn: str) -> None:
        self._engine = create_engine(dsn, future=True)

    def _rls(self, c, tenant_id: str) -> None:
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})

    # --- schemas ---
    def list_schemas(self, tenant_id: str):
        from newfan_gateway.records import SchemaFieldDef, SchemaRecord

        with self._engine.begin() as c:
            self._rls(c, tenant_id)
            rows = c.execute(
                text(
                    "SELECT DISTINCT ON (doc_type) id, doc_type, version, fields "
                    "FROM field_schemas WHERE tenant_id=:t ORDER BY doc_type, version DESC"
                ),
                {"t": tenant_id},
            ).all()
        return [
            SchemaRecord(
                id=r.id,
                tenant_id=tenant_id,
                doc_type=r.doc_type,
                version=r.version,
                fields=[SchemaFieldDef.model_validate(f) for f in (r.fields or [])],
            )
            for r in rows
        ]

    def get_schema(self, tenant_id: str, doc_type: str):
        from newfan_gateway.records import SchemaFieldDef, SchemaRecord

        with self._engine.begin() as c:
            self._rls(c, tenant_id)
            r = c.execute(
                text(
                    "SELECT id, version, fields FROM field_schemas "
                    "WHERE tenant_id=:t AND doc_type=:d ORDER BY version DESC LIMIT 1"
                ),
                {"t": tenant_id, "d": doc_type},
            ).first()
        if r is None:
            return None
        return SchemaRecord(
            id=r.id,
            tenant_id=tenant_id,
            doc_type=doc_type,
            version=r.version,
            fields=[SchemaFieldDef.model_validate(f) for f in (r.fields or [])],
        )

    def put_schema(self, tenant_id: str, doc_type: str, fields):
        import json as _json
        import uuid as _uuid

        from newfan_gateway.records import SchemaRecord

        payload = _json.dumps([f.model_dump() for f in fields], ensure_ascii=False)
        with self._engine.begin() as c:
            self._rls(c, tenant_id)
            nxt = c.execute(
                text(
                    "SELECT coalesce(max(version),0)+1 FROM field_schemas "
                    "WHERE tenant_id=:t AND doc_type=:d"
                ),
                {"t": tenant_id, "d": doc_type},
            ).scalar_one()
            sid = f"sch_{_uuid.uuid4().hex[:20]}"
            c.execute(
                text(
                    "INSERT INTO field_schemas (id, tenant_id, doc_type, version, fields) "
                    "VALUES (:i,:t,:d,:v, CAST(:f AS jsonb))"
                ),
                {"i": sid, "t": tenant_id, "d": doc_type, "v": nxt, "f": payload},
            )
        return SchemaRecord(id=sid, tenant_id=tenant_id, doc_type=doc_type, version=nxt, fields=list(fields))

    # --- rules ---
    def _rule(self, tenant_id: str, r):
        from newfan_gateway.records import RuleRecord

        return RuleRecord(
            id=r.id,
            tenant_id=tenant_id,
            doc_type=r.doc_type,
            supplier_key=r.supplier_key,
            field_name=r.field_name,
            rule_type=r.rule_type,
            rule_json=r.rule_json or {},
            status=r.status,
            validation_report=r.validation_report,
            source_correction_ids=r.source_correction_ids or [],
            created_by=r.created_by,
        )

    def list_rules(self, tenant_id: str, *, status=None, doc_type=None):
        with self._engine.begin() as c:
            self._rls(c, tenant_id)
            rows = c.execute(
                text(
                    "SELECT * FROM tenant_rules WHERE tenant_id=:t "
                    "AND (CAST(:s AS text) IS NULL OR status=CAST(:s AS text)) "
                    "AND (CAST(:d AS text) IS NULL OR doc_type IS NULL OR doc_type=CAST(:d AS text)) "
                    "ORDER BY created_at DESC"
                ),
                {"t": tenant_id, "s": status, "d": doc_type},
            ).all()
        return [self._rule(tenant_id, r) for r in rows]

    def get_rule(self, tenant_id: str, rule_id: str):
        with self._engine.begin() as c:
            self._rls(c, tenant_id)
            r = c.execute(
                text("SELECT * FROM tenant_rules WHERE tenant_id=:t AND id=:i"),
                {"t": tenant_id, "i": rule_id},
            ).first()
        return self._rule(tenant_id, r) if r else None

    def set_rule_status(self, tenant_id: str, rule_id: str, status: str):
        with self._engine.begin() as c:
            self._rls(c, tenant_id)
            c.execute(
                text("UPDATE tenant_rules SET status=:s, updated_at=now() WHERE tenant_id=:t AND id=:i"),
                {"s": status, "t": tenant_id, "i": rule_id},
            )
        return self.get_rule(tenant_id, rule_id)

    # --- metrics ---
    def list_memories(self, tenant_id: str, *, doc_type=None, field_name=None, limit=50):
        """§5.8 の修正メモリを人が読める形で返す。

        tenant_memories は faiss_vector_id しか持たないので、元の修正内容
        （correction_logs）と結合しないと「何を学習したのか」が分からない。
        """
        from newfan_gateway.records import MemoryRecord

        with self._engine.begin() as c:
            self._rls(c, tenant_id)
            rows = c.execute(
                text(
                    "SELECT m.id, m.tenant_id, m.correction_log_id, m.embed_model,"
                    " m.created_at, l.field_name, l.original_value, l.corrected_value,"
                    " l.doc_type, l.supplier_key, l.context, l.document_id "
                    "FROM tenant_memories m JOIN correction_logs l"
                    " ON l.id = m.correction_log_id "
                    "WHERE m.tenant_id=:t "
                    "AND (CAST(:d AS text) IS NULL OR l.doc_type=CAST(:d AS text)) "
                    "AND (CAST(:f AS text) IS NULL OR l.field_name=CAST(:f AS text)) "
                    "ORDER BY m.created_at DESC LIMIT :n"
                ),
                {"t": tenant_id, "d": doc_type, "f": field_name, "n": limit},
            ).mappings()
            return [
                MemoryRecord(
                    id=r["id"],
                    tenant_id=r["tenant_id"],
                    correction_log_id=r["correction_log_id"],
                    embed_model=r["embed_model"],
                    field_name=r["field_name"],
                    original_value=r["original_value"],
                    corrected_value=r["corrected_value"],
                    doc_type=r["doc_type"],
                    supplier_key=r["supplier_key"],
                    context=r["context"],
                    document_id=r["document_id"],
                    created_at=r["created_at"].isoformat() if r["created_at"] else None,
                )
                for r in rows
            ]

    def add_webhook_endpoint(self, tenant_id: str, *, url: str, secret: str, name: str):
        from newfan_gateway.records import ConnectionRecord

        rec_id = new_id("connection")
        with self._engine.begin() as c:
            self._rls(c, tenant_id)
            c.execute(
                text(
                    "INSERT INTO connections (id, tenant_id, type, name, config, status)"
                    " VALUES (:i,:t,'webhook',:n, CAST(:c AS jsonb), 'untested')"
                ),
                {
                    "i": rec_id,
                    "t": tenant_id,
                    "n": name,
                    "c": json.dumps({"url": url, "secret": secret}),
                },
            )
        return ConnectionRecord(
            id=rec_id, tenant_id=tenant_id, type="webhook", name=name,
            config={"url": url}, status="untested",
        )

    def list_webhook_endpoints(self, tenant_id: str):
        from newfan_gateway.records import ConnectionRecord

        with self._engine.begin() as c:
            self._rls(c, tenant_id)
            rows = c.execute(
                text(
                    "SELECT id, name, config, status, created_at FROM connections"
                    " WHERE tenant_id=:t AND type='webhook' ORDER BY created_at DESC"
                ),
                {"t": tenant_id},
            ).mappings().all()
        return [
            ConnectionRecord(
                id=r["id"],
                tenant_id=tenant_id,
                type="webhook",
                name=r["name"],
                # secret は返さない（登録時に一度だけ利用者が持つ。§6.4 の署名鍵）
                config={"url": (r["config"] or {}).get("url", "")},
                status=r["status"],
                created_at=r["created_at"].isoformat() if r["created_at"] else None,
            )
            for r in rows
        ]

    def metrics_summary(self, tenant_id: str):
        from newfan_gateway.records import MetricsSummary

        with self._engine.begin() as c:
            self._rls(c, tenant_id)
            total_docs = c.execute(
                text("SELECT count(*) FROM documents WHERE tenant_id=:t"), {"t": tenant_id}
            ).scalar_one()
            status_rows = c.execute(
                text("SELECT status, count(*) FROM extraction_runs WHERE tenant_id=:t GROUP BY status"),
                {"t": tenant_id},
            ).all()
            confirmed = c.execute(
                text("SELECT count(*) FROM extraction_runs WHERE tenant_id=:t AND status='confirmed'"),
                {"t": tenant_id},
            ).scalar_one()
            review = c.execute(
                text("SELECT count(*) FROM extraction_runs WHERE tenant_id=:t AND status='needs_review'"),
                {"t": tenant_id},
            ).scalar_one()
            stp_conf = c.execute(
                text(
                    "SELECT count(*) FROM extraction_runs r WHERE r.tenant_id=:t AND r.status='confirmed' "
                    "AND NOT EXISTS (SELECT 1 FROM correction_logs cl WHERE cl.run_id=r.id)"
                ),
                {"t": tenant_id},
            ).scalar_one()
            corrections = c.execute(
                text("SELECT count(*) FROM correction_logs WHERE tenant_id=:t"), {"t": tenant_id}
            ).scalar_one()
            active = c.execute(
                text("SELECT count(*) FROM tenant_rules WHERE tenant_id=:t AND status='active'"),
                {"t": tenant_id},
            ).scalar_one()
            pending = c.execute(
                text("SELECT count(*) FROM tenant_rules WHERE tenant_id=:t AND status IN ('draft','validating')"),
                {"t": tenant_id},
            ).scalar_one()
            memories = c.execute(
                text("SELECT count(*) FROM tenant_memories WHERE tenant_id=:t"), {"t": tenant_id}
            ).scalar_one()
        denom = (confirmed + review) or 1
        return MetricsSummary(
            total_documents=total_docs,
            status_counts={s: n for s, n in status_rows},
            stp_rate=round(stp_conf / denom, 4),
            corrections_total=corrections,
            active_rules=active,
            pending_rules=pending,
            memories_total=memories,
        )


class PgWorkflowsRepository:
    """WorkflowsRepository の PostgreSQL 実装（§16 設計 v0.2）。

    接続はアプリロール（newfan_app）。RLS（ENABLE+FORCE）が効く前提で、
    各トランザクションの先頭で app.tenant_id を設定する（§7.3）。
    """

    def __init__(self, dsn: str) -> None:
        self._engine = create_engine(dsn, future=True)

    def _rls(self, c, tenant_id: str) -> None:
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})

    @staticmethod
    def _row_to_record(r) -> WorkflowRecord:
        return WorkflowRecord(
            id=r["id"],
            tenant_id=r["tenant_id"],
            name=r["name"],
            status=r["status"],
            version=r["version"],
            graph_json=r["graph_json"] or {},
            auto_confirm=r["auto_confirm"],
            created_by=r["created_by"],
            updated_at=r["updated_at"].isoformat() if r["updated_at"] else None,
        )

    def list_workflows(self, tenant_id: str):
        with self._engine.begin() as c:
            self._rls(c, tenant_id)
            rows = c.execute(
                text(
                    "SELECT id, tenant_id, name, status, version, graph_json, auto_confirm,"
                    " created_by, updated_at FROM workflows WHERE tenant_id=:t"
                    " ORDER BY updated_at DESC"
                ),
                {"t": tenant_id},
            ).mappings().all()
        return [self._row_to_record(r) for r in rows]

    def get_workflow(self, tenant_id: str, workflow_id: str):
        with self._engine.begin() as c:
            self._rls(c, tenant_id)
            r = c.execute(
                text(
                    "SELECT id, tenant_id, name, status, version, graph_json, auto_confirm,"
                    " created_by, updated_at FROM workflows WHERE tenant_id=:t AND id=:i"
                ),
                {"t": tenant_id, "i": workflow_id},
            ).mappings().first()
        return self._row_to_record(r) if r else None

    def create_workflow(self, rec):
        with self._engine.begin() as c:
            self._rls(c, rec.tenant_id)
            c.execute(
                text(
                    "INSERT INTO workflows (id, tenant_id, name, status, version, graph_json,"
                    " auto_confirm, created_by)"
                    " VALUES (:i,:t,:n,'draft',1, CAST(:g AS jsonb), :a, :cb)"
                ),
                {
                    "i": rec.id,
                    "t": rec.tenant_id,
                    "n": rec.name,
                    "g": json.dumps(rec.graph_json, ensure_ascii=False),
                    "a": rec.auto_confirm,
                    "cb": rec.created_by,
                },
            )
        return self.get_workflow(rec.tenant_id, rec.id)

    def update_workflow(self, tenant_id: str, workflow_id: str, *, graph_json,
                        name=None, auto_confirm=None):
        with self._engine.begin() as c:
            self._rls(c, tenant_id)
            r = c.execute(
                text(
                    # 新版は必ず draft に戻す。「有効化＝版の固定」（§11.1）を守るため、
                    # active の定義を活性のまま差し替える経路を作らない。
                    "UPDATE workflows SET graph_json = CAST(:g AS jsonb),"
                    " name = COALESCE(:n, name),"
                    " auto_confirm = COALESCE(:a, auto_confirm),"
                    " version = version + 1, status = 'draft', updated_at = now()"
                    " WHERE tenant_id=:t AND id=:i RETURNING id"
                ),
                {
                    "g": json.dumps(graph_json, ensure_ascii=False),
                    "n": name,
                    "a": auto_confirm,
                    "t": tenant_id,
                    "i": workflow_id,
                },
            ).first()
        return self.get_workflow(tenant_id, workflow_id) if r else None

    def set_status(self, tenant_id: str, workflow_id: str, status: str):
        with self._engine.begin() as c:
            self._rls(c, tenant_id)
            r = c.execute(
                text(
                    "UPDATE workflows SET status=:s, updated_at=now()"
                    " WHERE tenant_id=:t AND id=:i RETURNING id"
                ),
                {"s": status, "t": tenant_id, "i": workflow_id},
            ).first()
        return self.get_workflow(tenant_id, workflow_id) if r else None

    def schema_exists(self, tenant_id: str, schema_id: str) -> bool:
        with self._engine.begin() as c:
            self._rls(c, tenant_id)
            r = c.execute(
                text("SELECT 1 FROM field_schemas WHERE tenant_id=:t AND id=:i"),
                {"t": tenant_id, "i": schema_id},
            ).first()
        return r is not None

    def connection_ok(self, tenant_id: str, connection_id: str) -> bool:
        # 疎通未確認（untested）の接続は有効化に使わせない（§16.5 の安全策）
        with self._engine.begin() as c:
            self._rls(c, tenant_id)
            r = c.execute(
                text(
                    "SELECT 1 FROM connections WHERE tenant_id=:t AND id=:i"
                    " AND status IN ('active','tested')"
                ),
                {"t": tenant_id, "i": connection_id},
            ).first()
        return r is not None

    def record_audit(self, tenant_id: str, *, actor_id: str, action: str,
                     target_id: str, detail) -> None:
        with self._engine.begin() as c:
            self._rls(c, tenant_id)
            c.execute(
                text(
                    "INSERT INTO audit_logs (id, tenant_id, actor_type, actor_id, action,"
                    " target_type, target_id, detail)"
                    " VALUES (:i,:t,'human',:a,:ac,'workflow',:tg, CAST(:d AS jsonb))"
                ),
                {
                    "i": new_id("audit"),
                    "t": tenant_id,
                    "a": actor_id,
                    "ac": action,
                    "tg": target_id,
                    "d": json.dumps(detail, ensure_ascii=False),
                },
            )
