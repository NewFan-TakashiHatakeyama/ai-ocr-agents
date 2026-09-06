# mypy: ignore-errors
"""PostgreSQL リポジトリ（本番 DB 接続 + RLS, §7 / §11）。

runtime extra（sqlalchemy）が必要。CI では未実行（DB 前提）。テーブルの正本は Alembic
マイグレーション（§15）。本モジュールの ORM モデルは gateway が参照する列のみをミラーする。

テナント分離: リクエスト毎に `SET LOCAL app.tenant_id` を発行し RLS を効かせる（§7.3）。
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator, Optional

from sqlalchemy import (
    JSON,
    DateTime,
    Integer,
    String,
    Text,
    create_engine,
    func,
    select,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from newfan_gateway.ids import new_id
from newfan_gateway.repository import DocumentGoneError
from newfan_gateway.records import (
    CorrectionRecord,
    DocumentRecord,
    JobRecord,
    PageRecord,
    RunRecord,
    WorkflowNodeRunRecord,
    WorkflowRecord,
    WorkflowRunRecord,
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
    # 「最新の run」を決めるために要る。DDL 側に DEFAULT now() があるので INSERT では
    # 渡さない（渡すとアプリのクロックと DB のクロックが混ざる）。
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
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

    def supersede_review_runs(self, tenant_id, document_id) -> int:
        # needs_review のまま残った run を終端させる。processing は触らない
        # （実行中の worker が finalize しようとしている）。
        # PgRepository._rls は「セッションを yield する contextmanager」であり、
        # PgAdminRepository._rls（接続に SET を発行するだけ）とは形が違う。
        with self._rls(tenant_id) as s:
            res = s.execute(
                text(
                    "UPDATE extraction_runs SET status='superseded', finished_at=now() "
                    "WHERE tenant_id=:t AND document_id=:d AND status='needs_review'"
                ),
                {"t": tenant_id, "d": document_id},
            )
            return int(res.rowcount or 0)

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
        # **id 順ではなく開始時刻順**。run id は `run_` + ランダム uuid なので、
        # id.desc() は「最新」ではなく実質ランダムに 1 本を選ぶ。帳票に run が
        # 1 本しか無い間は表面化しないが、チャットの再抽出や supersede 付き
        # 再抽出で 2 本目ができた瞬間、検証画面が古い結果を表示し始める
        # （実機で 4 回中 1 回再現した）。同時刻の並びは id で決定論化する。
        with self._rls(tenant_id) as s:
            stmt = (
                select(ExtractionRun)
                .where(ExtractionRun.document_id == document_id)
                .order_by(ExtractionRun.started_at.desc(), ExtractionRun.id.desc())
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
                " validation, review_status, label"
                " FROM extraction_fields WHERE run_id=:r ORDER BY field_name"
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
                    # スキーマ定義の label を正とし、無ければ行の label
                    # （スキーマレス自動発見で LLM が申告した見出し原文）を使う
                    "label": labels.get(r.field_name) or r.label,
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
            region_stats=(row.metrics or {}).get("region"),
        )

    def set_document_status(self, tenant_id, document_id, status):
        # updated_at も必ず進める。ORM モデルは gateway が参照する列だけをミラーする
        # 方針で updated_at を持たないため、生 SQL で書く。ここを更新しないと
        # get_delete_blocker の「確定処理中の窓」判定（updated_at 基準）が
        # gateway 由来の遷移に対して永久に発火せず、無言で素通りする。
        with self._rls(tenant_id) as s:
            s.execute(
                text(
                    "UPDATE documents SET status=:s, updated_at=now()"
                    " WHERE tenant_id=:t AND id=:d"
                ),
                {"s": status, "t": tenant_id, "d": document_id},
            )

    def set_document_doc_type(self, tenant_id, document_id, doc_type):
        # set_document_status と違い **updated_at を進めない**（Protocol の docstring
        # 参照）。意図を SQL の見た目で示すため ORM ではなく生 SQL に揃える。
        # RLS ポリシーは USING のみで WITH CHECK が無いため、WHERE から tenant_id を
        # 落とすと DB 側は他テナント行の更新を止めない。WHERE が唯一の防御になる。
        with self._rls(tenant_id) as s:
            s.execute(
                text("UPDATE documents SET doc_type=:dt WHERE tenant_id=:t AND id=:d"),
                {"dt": doc_type, "t": tenant_id, "d": document_id},
            )

    # --- 削除（§6.2 DELETE /documents/{id}） ---

    def get_delete_blocker(self, tenant_id, document_id, *, stale_minutes):
        """削除を止める理由。消せるなら None。

        documents.status を先に見るのが要点。confirm（routers.py）は documents を
        in_review にするだけで extraction_runs は needs_review のまま残し、しかも
        ロックを解放する。run.status だけ見ると確定処理の最中に削除が通り、
        直後に resume したワーカーが孤児の correction_logs を作って webhook を
        外部へ飛ばす。

        いずれも stale_minutes より古いものは「停止したまま固着した」とみなして
        通す。mark_run_failed はワーカーの例外ハンドラ経由でしか呼ばれず、
        Fargate のタスク入れ替えや OOM では processing が永久に残るため、
        閾値が無いと「消したい帳票ほど消せない」になる。
        """
        with self._rls(tenant_id) as s:
            row = s.execute(
                text(
                    "SELECT status FROM documents"
                    " WHERE tenant_id=:t AND id=:d"
                    "   AND status IN ('queued','processing','in_review')"
                    "   AND updated_at > now() - make_interval(mins => :m)"
                ),
                {"t": tenant_id, "d": document_id, "m": stale_minutes},
            ).first()
            if row is not None:
                return "document_busy"
            row = s.execute(
                text(
                    "SELECT 1 FROM extraction_runs"
                    " WHERE tenant_id=:t AND document_id=:d AND status='processing'"
                    "   AND started_at > now() - make_interval(mins => :m)"
                ),
                {"t": tenant_id, "d": document_id, "m": stale_minutes},
            ).first()
            return "processing" if row is not None else None

    def delete_document(self, tenant_id, document_id, *, actor_id, detail):
        """帳票と派生データを 1 トランザクションで消す。

        FK CASCADE が効くのは pages / extraction_runs（→ extraction_fields /
        extraction_tables）だけ。correction_logs・jobs・workflow_runs は
        documents を TEXT 列で指すだけなので DB は何もしてくれない（0001 の
        :154 / :206 / :265）。ここで明示的に消さないと、消したはずの原本の値が
        correction_logs に残り、jobs は参照先を失って無限に再配信される。

        audit_logs への記録も同一トランザクションに含める。別トランザクションだと
        「消えたのに痕跡が無い」が成立し得る。audit_logs はアプリロールから
        DELETE できない（ensure_app_role.py）ので、これが唯一の恒久証跡になる。

        RLS に加えて全文へ tenant_id=:t を明示する（PgWorkflowsRepository と同じ
        二重防御）。
        """
        counts: dict[str, int] = {}
        with self._rls(tenant_id) as s:
            # ワーカーの FOR NO KEY UPDATE と噛み合ったら待たずに落とす。
            # 待つと gateway のワーカースレッドが専有され、他のリクエストも巻き添えになる。
            s.execute(text("SET LOCAL lock_timeout = '3s'"))
            doc = s.execute(
                text(
                    "SELECT id, status, doc_type, page_count, original_name, storage_uri,"
                    " external_ref FROM documents WHERE tenant_id=:t AND id=:d"
                ),
                {"t": tenant_id, "d": document_id},
            ).mappings().first()
            if doc is None:
                return None

            run_ids = [
                r[0]
                for r in s.execute(
                    text(
                        "SELECT id FROM extraction_runs WHERE tenant_id=:t AND document_id=:d"
                    ),
                    {"t": tenant_id, "d": document_id},
                ).all()
            ]

            # 1) 学習ソース。tenant_memories は correction_logs の FK CASCADE で消える
            counts["corrections_deleted"] = s.execute(
                text("DELETE FROM correction_logs WHERE tenant_id=:t AND document_id=:d"),
                {"t": tenant_id, "d": document_id},
            ).rowcount

            counts["runs_deleted"] = len(run_ids)
            counts["jobs_deleted"] = 0
            counts["checkpoints_deleted"] = 0
            if run_ids:
                # 2) jobs は document_id を持たず ref_id が run_id を指す（0001:206）。
                #    extraction_runs の CASCADE より先に解決する必要がある
                #    （消えたあとでは run_id を引けない）。
                counts["jobs_deleted"] = s.execute(
                    text("DELETE FROM jobs WHERE tenant_id=:t AND ref_id = ANY(:r)"),
                    {"t": tenant_id, "r": run_ids},
                ).rowcount

                # 3) 抽出グラフの LangGraph チェックポイント（thread_id = run_id）。
                #    テナント列を持たない外部スキーマなので、表の存在を確認してから触る
                #    （PostgresSaver.setup 未実行の環境では表ごと無い）。
                if s.execute(text("SELECT to_regclass('public.checkpoints')")).scalar():
                    for tbl in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                        counts["checkpoints_deleted"] += s.execute(
                            text(f"DELETE FROM public.{tbl} WHERE thread_id = ANY(:r)"),
                            {"r": run_ids},
                        ).rowcount

            # 4) ワークフロー実行履歴は残す（運用の証跡）。ただし帳票由来の中身は消す。
            #    node_runs の input/output には抽出値がそのまま入る。
            s.execute(
                text(
                    "UPDATE workflow_node_runs SET input=NULL, output=NULL"
                    " WHERE tenant_id=:t AND workflow_run_id IN"
                    "   (SELECT id FROM workflow_runs WHERE tenant_id=:t AND document_id=:d)"
                ),
                {"t": tenant_id, "d": document_id},
            )
            # 5) 帳票へのリンクを切る。未終端（waiting_hitl / running）は再開先が
            #    消えた以上続行不能なので failed に終端化する。
            #    running も含めるのが要点: 事前の has_running_workflow_run は別
            #    トランザクションなので、チェック通過後に waiting_hitl → running へ
            #    転移する窓がある。ここで拾わないと document_id=NULL のまま永久に
            #    running で残り、list_hitl_boosts やワークフロー画面を汚し続ける。
            counts["workflow_runs_detached"] = s.execute(
                text(
                    "UPDATE workflow_runs SET"
                    " document_id = NULL,"
                    " state = COALESCE(state,'{}'::jsonb) || '{\"document_deleted\": true}'::jsonb,"
                    " status = CASE WHEN status IN ('waiting_hitl','running')"
                    "   THEN 'failed' ELSE status END,"
                    " error = CASE WHEN status IN ('waiting_hitl','running')"
                    "   THEN '{\"code\":\"E1001\",\"message\":\"document deleted\"}'::jsonb"
                    "   ELSE error END,"
                    " finished_at = CASE WHEN status IN ('waiting_hitl','running')"
                    "   THEN now() ELSE finished_at END"
                    " WHERE tenant_id=:t AND document_id=:d"
                ),
                {"t": tenant_id, "d": document_id},
            ).rowcount

            # 6) 監査は削除本体と同じトランザクションで
            s.execute(
                text(
                    "INSERT INTO audit_logs (id, tenant_id, actor_type, actor_id, action,"
                    " target_type, target_id, detail)"
                    " VALUES (:i,:t,'human',:a,'document.delete','document',:d, CAST(:j AS jsonb))"
                ),
                {
                    "i": new_id("audit"),
                    "t": tenant_id,
                    "a": actor_id,
                    "d": document_id,
                    "j": json.dumps(
                        {
                            **detail,
                            **counts,
                            "status": doc["status"],
                            "doc_type": doc["doc_type"],
                            "page_count": doc["page_count"],
                            "original_name": doc["original_name"],
                            "storage_uri": doc["storage_uri"],
                            "external_ref": doc["external_ref"],
                            "run_ids": run_ids,
                        },
                        ensure_ascii=False,
                    ),
                },
            )

            # 7) 本体。CASCADE で pages / extraction_runs → fields / tables が消える
            deleted = s.execute(
                text("DELETE FROM documents WHERE tenant_id=:t AND id=:d"),
                {"t": tenant_id, "d": document_id},
            ).rowcount
            if deleted != 1:
                # 同時削除で先を越された。監査行ごとロールバックして「消していない」に戻す。
                # 専用例外にするのは、router が「不在」として 400/E1001 に翻訳できる
                # ようにするため（汎用例外だと 500「内部エラー」になり、利用者には
                # 何が起きたか伝わらない）。
                raise DocumentGoneError(document_id)
        return counts

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
        """修正ログを追記する。削除済み帳票宛ての分は静かに捨てる。

        0005 で correction_logs → documents に FK を張ったため、削除された帳票へ
        INSERT すると ForeignKeyViolation になる。オートセーブ（500ms デバウンス）と
        削除は容易に競合するので、例外にせず WHERE EXISTS で弾く。残しても
        「消したはずの原本の値」が DB に復活するだけで、誰の役にも立たない。
        """
        if not corrections:
            return
        with self._rls(corrections[0].tenant_id) as s:
            for c in corrections:
                s.execute(
                    text(
                        "INSERT INTO correction_logs (id, tenant_id, document_id, run_id,"
                        " field_name, original_value, corrected_value, doc_type,"
                        " supplier_key, context, reviewer_id)"
                        " SELECT :i,:t,:d,:r,:f,:ov,:cv,:dt,:sk,:cx,:rv"
                        " WHERE EXISTS (SELECT 1 FROM documents"
                        "   WHERE id=:d AND tenant_id=:t)"
                    ),
                    {
                        "i": c.id, "t": c.tenant_id, "d": c.document_id, "r": c.run_id,
                        "f": c.field_name, "ov": c.original_value, "cv": c.corrected_value,
                        # 学習ループ（DD-06/DD-07）の検索キーと embedding 入力
                        "dt": c.doc_type, "sk": c.supplier_key,
                        "cx": c.context, "rv": c.reviewer_id,
                    },
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

    def list_hitl_boosts(self, tenant_id):
        # hitl_gate の interrupt payload（priority_boost 含む）は runner が
        # workflow_runs.state JSONB の "waiting" キーに永続化する（workflow_store._update。
        # 独立した waiting 列は存在しない）。待機中の run だけが加点対象
        with self._rls(tenant_id) as s:
            rows = s.execute(
                text(
                    "SELECT document_id,"
                    " MAX(COALESCE((state->'waiting'->>'priority_boost')::int, 0))"
                    " FROM workflow_runs"
                    " WHERE status='waiting_hitl' AND document_id IS NOT NULL"
                    " GROUP BY document_id"
                )
            ).all()
        return {r[0]: int(r[1] or 0) for r in rows}


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
        region_stats=(row.metrics or {}).get("region"),
    )


# ============ 管理画面（SCR-04/05/06）Pg 実装 ============


def schema_fields_payload(fields) -> list:
    """field_schemas.fields へ書く JSON 構造を作る（設計 §4.7 / C27・C29）。

    region が未設定の field では **region キー自体を書かない**。素直に
    ``model_dump()`` すると ``"region": null`` が JSONB に入り、旧 orchestrator の
    ``make_kie_extract`` が schema を丸ごと ``json.dumps`` でプロンプトに載せるため、
    「領域を使っていないスキーマのプロンプトは 1 バイトも変わらない」という
    受け入れ条件が gateway 先行デプロイだけで破れる（2 サービスのローリング完了順は
    保証されない）。

    ``exclude_none=True`` の全体適用は**不可**——既存の ``"label": null`` /
    ``"columns": null`` まで消えてしまい、それ自体が現行のプロンプトを変える。
    落とすのは region キーだけに限定する。
    """
    return [
        f.model_dump(exclude={"region"}) if f.region is None else f.model_dump()
        for f in fields
    ]


class PgAdminRepository:
    """AdminRepository の PostgreSQL 実装（field_schemas / tenant_rules / 集計）。"""

    def __init__(self, dsn: str) -> None:
        self._engine = create_engine(dsn, future=True)

    def _rls(self, c, tenant_id: str) -> None:
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})

    # --- schemas ---
    def get_schema_by_id(self, tenant_id: str, schema_id: str):
        """id 直引き（旧版も解決する）。extract の schema_id 検証に使う。

        列は実 DDL（0001: field_schemas に updated_at は無く created_at のみ）に
        合わせる。存在しない列を書くと InMemory テストは通るのに本番だけ
        UndefinedColumn → 500 になる（correction_logs の note 事故と同型。実 AWS で再発）。
        """
        from newfan_gateway.records import SchemaFieldDef, SchemaRecord

        with self._engine.begin() as c:
            self._rls(c, tenant_id)
            r = c.execute(
                text(
                    "SELECT id, tenant_id, doc_type, version, fields,"
                    " exclude_regions, source_page_count"
                    " FROM field_schemas WHERE tenant_id=:t AND id=:i"
                ),
                {"t": tenant_id, "i": schema_id},
            ).mappings().first()
        if r is None:
            return None
        return SchemaRecord(
            id=r["id"], tenant_id=r["tenant_id"], doc_type=r["doc_type"],
            version=r["version"],
            fields=[SchemaFieldDef(**f) for f in (r["fields"] or [])],
            exclude_regions=list(r["exclude_regions"] or []),
            source_page_count=r["source_page_count"],
        )

    def list_schemas(self, tenant_id: str):
        from newfan_gateway.records import SchemaFieldDef, SchemaRecord

        with self._engine.begin() as c:
            self._rls(c, tenant_id)
            rows = c.execute(
                text(
                    "SELECT DISTINCT ON (doc_type) id, doc_type, version, fields, "
                    "exclude_regions, source_page_count "
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
                exclude_regions=list(r.exclude_regions or []),
                source_page_count=r.source_page_count,
            )
            for r in rows
        ]

    def get_schema(self, tenant_id: str, doc_type: str):
        from newfan_gateway.records import SchemaFieldDef, SchemaRecord

        with self._engine.begin() as c:
            self._rls(c, tenant_id)
            r = c.execute(
                text(
                    "SELECT id, version, fields, exclude_regions, source_page_count "
                    "FROM field_schemas "
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
            exclude_regions=list(r.exclude_regions or []),
            source_page_count=r.source_page_count,
        )

    def put_schema(
        self, tenant_id: str, doc_type: str, fields, *, exclude_regions=None, source_page_count=None
    ):
        import json as _json
        import uuid as _uuid

        from newfan_gateway.records import SchemaRecord

        payload = _json.dumps(schema_fields_payload(fields), ensure_ascii=False)
        with self._engine.begin() as c:
            self._rls(c, tenant_id)
            # 引き継ぎ元は**版採番と同一トランザクション内**で、get_schema と同じ
            # 版選択（ORDER BY version DESC LIMIT 1）で取る。ORDER BY を落とすと
            # v1 の設定が復活して v2 以降の設定が消えるという、InMemory では
            # 検出できない事故になる（設計 §4.4 / C22）。
            prev = c.execute(
                text(
                    "SELECT exclude_regions, source_page_count FROM field_schemas "
                    "WHERE tenant_id=:t AND doc_type=:d ORDER BY version DESC LIMIT 1"
                ),
                {"t": tenant_id, "d": doc_type},
            ).first()
            # None = 引き継ぎ / 明示 [] = クリア（§4.4）
            if exclude_regions is not None:
                regions = [
                    r if isinstance(r, dict) else r.model_dump() for r in exclude_regions
                ]
            else:
                regions = list(prev[0] or []) if prev is not None else []
            if source_page_count is not None:
                pages = source_page_count
            else:
                pages = prev[1] if prev is not None else None

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
                    "INSERT INTO field_schemas "
                    "(id, tenant_id, doc_type, version, fields, exclude_regions, source_page_count)"
                    " VALUES (:i,:t,:d,:v, CAST(:f AS jsonb), CAST(:x AS jsonb), :p)"
                ),
                {
                    "i": sid, "t": tenant_id, "d": doc_type, "v": nxt, "f": payload,
                    "x": _json.dumps(regions, ensure_ascii=False), "p": pages,
                },
            )
        # 戻り値は引数由来ではなく **INSERT した確定値**（引き継ぎ後）にする。
        # PUT 応答＝直後の GET 応答でないと、旧編集画面が空配列で state を上書きし、
        # 次の保存で明示 []（＝本当のクリア）を送る誘発経路になる（§4.4 / C28）。
        return SchemaRecord(
            id=sid,
            tenant_id=tenant_id,
            doc_type=doc_type,
            version=nxt,
            fields=list(fields),
            exclude_regions=regions,
            source_page_count=pages,
        )

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

    def create_rule(self, rec):
        import json

        with self._engine.begin() as c:
            self._rls(c, rec.tenant_id)
            c.execute(
                text(
                    "INSERT INTO tenant_rules (id, tenant_id, doc_type, supplier_key, field_name,"
                    " rule_type, rule_json, status, validation_report, source_correction_ids, created_by)"
                    " VALUES (:id, :t, :dt, :sk, :fn, :rt, CAST(:rj AS jsonb), :st,"
                    " CAST(:vr AS jsonb), CAST(:sc AS jsonb), :cb)"
                ),
                {
                    "id": rec.id,
                    "t": rec.tenant_id,
                    "dt": rec.doc_type,
                    "sk": rec.supplier_key,
                    "fn": rec.field_name,
                    "rt": rec.rule_type,
                    "rj": json.dumps(rec.rule_json or {}),
                    "st": rec.status,
                    "vr": json.dumps(rec.validation_report) if rec.validation_report else None,
                    "sc": json.dumps(rec.source_correction_ids or []),
                    "cb": rec.created_by,
                },
            )
        return self.get_rule(rec.tenant_id, rec.id)

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

    def add_webhook_endpoint(
        self, tenant_id: str, *, url: str, secret, name: str, secret_ref=None
    ):
        from newfan_gateway.records import ConnectionRecord

        rec_id = new_id("connection")
        # secret_ref があれば署名鍵は Secrets Manager 参照のみ（§16.5 / P6）。
        # 無ければ旧方式（config.secret 平文）に fallback（ローカル・移行期間）
        config = {"url": url} if secret_ref else {"url": url, "secret": secret}
        with self._engine.begin() as c:
            self._rls(c, tenant_id)
            c.execute(
                text(
                    "INSERT INTO connections (id, tenant_id, type, name, config,"
                    " secret_ref, status)"
                    " VALUES (:i,:t,'webhook',:n, CAST(:c AS jsonb), :sr, 'untested')"
                ),
                {
                    "i": rec_id,
                    "t": tenant_id,
                    "n": name,
                    "c": json.dumps(config),
                    "sr": secret_ref,
                },
            )
        return ConnectionRecord(
            id=rec_id, tenant_id=tenant_id, type="webhook", name=name,
            config={"url": url}, secret_ref=secret_ref, status="untested",
        )

    def create_connection(
        self, tenant_id, *, type, name, config, secret_ref, allowed_tables
    ):
        from newfan_gateway.records import ConnectionRecord

        rec_id = new_id("connection")
        with self._engine.begin() as c:
            self._rls(c, tenant_id)
            c.execute(
                text(
                    "INSERT INTO connections (id, tenant_id, type, name, config,"
                    " secret_ref, allowed_tables, status)"
                    " VALUES (:i,:t,:ty,:n, CAST(:c AS jsonb), :sr, CAST(:at AS jsonb),"
                    " 'untested')"
                ),
                {
                    "i": rec_id, "t": tenant_id, "ty": type, "n": name,
                    "c": json.dumps(config, ensure_ascii=False),
                    "sr": secret_ref,
                    "at": json.dumps(list(allowed_tables)),
                },
            )
        return ConnectionRecord(
            id=rec_id, tenant_id=tenant_id, type=type, name=name, config=dict(config),
            secret_ref=secret_ref, allowed_tables=list(allowed_tables), status="untested",
        )

    def _connection_record(self, r):
        from newfan_gateway.records import ConnectionRecord

        return ConnectionRecord(
            id=r["id"], tenant_id=r["tenant_id"], type=r["type"], name=r["name"],
            config=r["config"] or {}, secret_ref=r["secret_ref"],
            allowed_tables=list(r["allowed_tables"] or []), status=r["status"],
            created_at=r["created_at"].isoformat() if r["created_at"] else None,
            last_synced_at=r["last_synced_at"].isoformat() if r["last_synced_at"] else None,
            last_sync_status=r["last_sync_status"],
            last_sync_error=r["last_sync_error"],
        )

    def list_connections(self, tenant_id):
        with self._engine.begin() as c:
            self._rls(c, tenant_id)
            rows = c.execute(
                text(
                    "SELECT id, tenant_id, type, name, config, secret_ref,"
                    " allowed_tables, status, created_at,"
                    " last_synced_at, last_sync_status, last_sync_error"
                    " FROM connections WHERE tenant_id=:t ORDER BY created_at DESC"
                ),
                {"t": tenant_id},
            ).mappings().all()
        return [self._connection_record(r) for r in rows]

    def get_connection(self, tenant_id, connection_id):
        with self._engine.begin() as c:
            self._rls(c, tenant_id)
            r = c.execute(
                text(
                    "SELECT id, tenant_id, type, name, config, secret_ref,"
                    " allowed_tables, status, created_at,"
                    " last_synced_at, last_sync_status, last_sync_error"
                    " FROM connections WHERE tenant_id=:t AND id=:i"
                ),
                {"t": tenant_id, "i": connection_id},
            ).mappings().first()
        return self._connection_record(r) if r else None

    def set_connection_status(self, tenant_id, connection_id, status):
        with self._engine.begin() as c:
            self._rls(c, tenant_id)
            c.execute(
                text("UPDATE connections SET status=:s WHERE tenant_id=:t AND id=:i"),
                {"s": status, "t": tenant_id, "i": connection_id},
            )
        return self.get_connection(tenant_id, connection_id)

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
            # フィールド精度: 確定済み Run（直近 30 日）の「修正されなかったフィールド割合」。
            # 設計 §12 の週次サンプル監査（人手）の代替として、実データから機械算出する
            acc = c.execute(
                text(
                    "SELECT count(*),"
                    " count(*) FILTER (WHERE ef.correction IS NOT NULL)"
                    " FROM extraction_fields ef JOIN extraction_runs r ON r.id = ef.run_id"
                    " WHERE r.tenant_id=:t AND r.status='confirmed'"
                    " AND r.started_at > now() - interval '30 days'"
                ),
                {"t": tenant_id},
            ).first()
            field_accuracy = (
                1.0 - (acc[1] / acc[0]) if acc is not None and acc[0] else None
            )
            # LLM コスト: run 単位に永続化した実測トークン × 単価の合計（直近 30 日）。
            # 計測データを持つ run が 1 件も無ければ None（ダッシュボードは「—」表示）
            cost_row = c.execute(
                text(
                    "SELECT sum((metrics->>'llm_cost_jpy')::numeric),"
                    " count(*) FILTER (WHERE metrics ? 'llm_cost_jpy')"
                    " FROM extraction_runs WHERE tenant_id=:t"
                    " AND started_at > now() - interval '30 days'"
                ),
                {"t": tenant_id},
            ).first()
            llm_cost = float(cost_row[0]) if cost_row and cost_row[1] else None
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
            field_accuracy_sampled=field_accuracy,
            llm_cost_jpy_total=llm_cost,
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

    def schema_is_latest(self, tenant_id: str, schema_id: str) -> bool:
        """この schema_id が当該 doc_type の最新版か（lint L012）。

        存在しない id は True を返す（「最新でない」ではなく「存在しない」であり、
        それは L009 が error として出す。ここで二重に出すと指摘が読みにくい）。
        """
        with self._engine.begin() as c:
            self._rls(c, tenant_id)
            r = c.execute(
                text(
                    "SELECT s.id = ("
                    "  SELECT id FROM field_schemas"
                    "  WHERE tenant_id = s.tenant_id AND doc_type = s.doc_type"
                    "  ORDER BY version DESC LIMIT 1"
                    ") FROM field_schemas s WHERE s.tenant_id=:t AND s.id=:i"
                ),
                {"t": tenant_id, "i": schema_id},
            ).first()
        return True if r is None else bool(r[0])

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

    # --- 実行（§16 設計 v0.2 §11 / P3） ---

    def create_run(self, rec):
        with self._engine.begin() as c:
            self._rls(c, rec.tenant_id)
            c.execute(
                text(
                    "INSERT INTO workflow_runs (id, tenant_id, workflow_id, workflow_version,"
                    " trigger, document_id, state, status)"
                    " VALUES (:i,:t,:w,:v, CAST(:tr AS jsonb), :d, '{}'::jsonb, 'running')"
                ),
                {
                    "i": rec.id,
                    "t": rec.tenant_id,
                    "w": rec.workflow_id,
                    "v": rec.workflow_version,
                    "tr": json.dumps(rec.trigger, ensure_ascii=False),
                    "d": rec.document_id,
                },
            )
        return self.get_run(rec.tenant_id, rec.id)

    @staticmethod
    def _run_record(r) -> WorkflowRunRecord:
        return WorkflowRunRecord(
            id=r["id"],
            tenant_id=r["tenant_id"],
            workflow_id=r["workflow_id"],
            workflow_version=r["workflow_version"],
            document_id=r["document_id"],
            # trigger には graph_json スナップショット（版固定, §11.1）が入っており大きい。
            # API 応答には出さない（dto 層で落とす）が、record としては保持する
            trigger=r["trigger"] or {},
            state=r["state"] or {},
            status=r["status"],
            error=r["error"],
            started_at=r["started_at"].isoformat() if r["started_at"] else None,
            finished_at=r["finished_at"].isoformat() if r["finished_at"] else None,
        )

    def get_run(self, tenant_id: str, run_id: str):
        with self._engine.begin() as c:
            self._rls(c, tenant_id)
            r = c.execute(
                text(
                    "SELECT id, tenant_id, workflow_id, workflow_version, document_id,"
                    " trigger, state, status, error, started_at, finished_at"
                    " FROM workflow_runs WHERE tenant_id=:t AND id=:i"
                ),
                {"t": tenant_id, "i": run_id},
            ).mappings().first()
        return self._run_record(r) if r else None

    def list_runs(self, tenant_id: str, workflow_id: str, *, status=None, limit=50):
        with self._engine.begin() as c:
            self._rls(c, tenant_id)
            rows = c.execute(
                text(
                    "SELECT id, tenant_id, workflow_id, workflow_version, document_id,"
                    " trigger, state, status, error, started_at, finished_at"
                    " FROM workflow_runs WHERE tenant_id=:t AND workflow_id=:w"
                    " AND (CAST(:s AS text) IS NULL OR status=CAST(:s AS text))"
                    " ORDER BY started_at DESC LIMIT :n"
                ),
                {"t": tenant_id, "w": workflow_id, "s": status, "n": limit},
            ).mappings().all()
        return [self._run_record(r) for r in rows]

    def list_node_runs(self, tenant_id: str, run_id: str):
        with self._engine.begin() as c:
            self._rls(c, tenant_id)
            rows = c.execute(
                text(
                    "SELECT node_id, node_type, status, attempt, output, error,"
                    " started_at, finished_at FROM workflow_node_runs"
                    " WHERE tenant_id=:t AND workflow_run_id=:r ORDER BY started_at NULLS LAST"
                ),
                {"t": tenant_id, "r": run_id},
            ).mappings().all()
        return [
            WorkflowNodeRunRecord(
                node_id=r["node_id"],
                node_type=r["node_type"],
                status=r["status"],
                attempt=r["attempt"],
                output=r["output"],
                error=r["error"],
                started_at=r["started_at"].isoformat() if r["started_at"] else None,
                finished_at=r["finished_at"].isoformat() if r["finished_at"] else None,
            )
            for r in rows
        ]

    def has_running_workflow_run(self, tenant_id: str, document_id: str) -> bool:
        # waiting_hitl は含めない。止める API が無いので含めると永久に削除できなくなる
        # （削除側で failed に終端化する）。
        with self._engine.begin() as c:
            self._rls(c, tenant_id)
            r = c.execute(
                text(
                    "SELECT 1 FROM workflow_runs"
                    " WHERE tenant_id=:t AND document_id=:d AND status='running' LIMIT 1"
                ),
                {"t": tenant_id, "d": document_id},
            ).first()
        return r is not None
