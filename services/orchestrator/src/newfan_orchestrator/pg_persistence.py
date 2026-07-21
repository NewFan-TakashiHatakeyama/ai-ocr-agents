# mypy: ignore-errors
"""PgContextStore（本番）: PostgreSQL 実装（§7 / §4.3）。

runtime 依存（sqlalchemy + psycopg）。RLS 用に SET LOCAL app.tenant_id を発行する
（テーブル所有者ロールは既定で RLS を bypass。本番は非所有ロール＋FORCE RLS を推奨）。
"""

from __future__ import annotations

import json
import uuid

from sqlalchemy import create_engine, text

from newfan_orchestrator.persistence import LoadedContext

# schema 未割当（テンプレートレス既定）の run に渡す placeholder。
# FieldSchema.doc_type は str 必須のため None を入れると deterministic_normalize が
# ValidationError で落ちる（実コンテナの E2E で検出）。nodes 側の既定と同じ "" に揃える。
EMPTY_SCHEMA: dict = {"doc_type": "", "fields": []}


class PgContextStore:
    def __init__(self, dsn: str) -> None:
        self._engine = create_engine(dsn, future=True)

    def load_context(self, tenant_id: str, run_id: str):
        with self._engine.begin() as c:
            c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
            run = c.execute(
                text("SELECT document_id, schema_id FROM extraction_runs WHERE id = :r"),
                {"r": run_id},
            ).first()
            if run is None:
                return None
            document_id, schema_id = run
            schema = dict(EMPTY_SCHEMA)
            if schema_id is not None:
                srow = c.execute(
                    text("SELECT doc_type, fields FROM field_schemas WHERE id = :s"),
                    {"s": schema_id},
                ).first()
                if srow is not None:
                    schema = {"doc_type": srow[0], "fields": srow[1]}
            pages = [
                {"page_no": r[0], "image_uri": r[1], "width": r[2], "height": r[3]}
                for r in c.execute(
                    text(
                        "SELECT page_no, image_uri, width, height FROM pages "
                        "WHERE document_id = :d ORDER BY page_no"
                    ),
                    {"d": document_id},
                )
            ]
            return LoadedContext(document_id=document_id, schema=schema, pages=pages)

    def save_result(
        self, tenant_id, run_id, *, fields, tables, review_items, status, fallback_pages=None
    ) -> None:
        with self._engine.begin() as c:
            c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
            for f in fields:
                c.execute(
                    text(
                        "INSERT INTO extraction_fields "
                        "(id, tenant_id, run_id, field_name, value_raw, value_normalized, "
                        " final_value, confidence, grounding_score, page_no, bbox, source_quote, "
                        " span_ids, review_status) "
                        "VALUES (:id,:t,:r,:fn,:vr,:vn,:fv,:cf,:gs,:pg, CAST(:bb AS jsonb), :sq, "
                        " CAST(:si AS jsonb), :rs) "
                        "ON CONFLICT (run_id, field_name) DO UPDATE SET "
                        " value_normalized = EXCLUDED.value_normalized, "
                        " final_value = EXCLUDED.final_value, "
                        " confidence = EXCLUDED.confidence, "
                        " grounding_score = EXCLUDED.grounding_score, "
                        " review_status = EXCLUDED.review_status"
                    ),
                    {
                        "id": f"fld_{uuid.uuid4().hex[:20]}",
                        "t": tenant_id,
                        "r": run_id,
                        "fn": f.name,
                        "vr": f.value_raw,
                        "vn": f.value_normalized,
                        "fv": f.value_normalized,
                        "cf": f.confidence,
                        "gs": f.grounding_score,
                        "pg": f.page,
                        "bb": json.dumps(f.bbox) if f.bbox is not None else None,
                        "sq": f.source_quote,
                        "si": json.dumps(f.span_ids),
                        "rs": f.review_status.value,
                    },
                )
            # 明細テーブル（構造由来, §5.3）を extraction_tables に永続化。冪等のため run 分を洗替。
            c.execute(text("DELETE FROM extraction_tables WHERE run_id = :r"), {"r": run_id})
            for t in tables:
                rows_json = [
                    {
                        col: {"value": cell.value, "span_ids": cell.span_ids, "bbox": cell.bbox}
                        for col, cell in row.items()
                    }
                    for row in t.rows
                ]
                c.execute(
                    text(
                        "INSERT INTO extraction_tables "
                        "(id, tenant_id, run_id, name, page_no, structure_html, rows, confidence) "
                        "VALUES (:id,:t,:r,:nm,:pg,:html, CAST(:rows AS jsonb), :cf)"
                    ),
                    {
                        "id": f"tbl_{uuid.uuid4().hex[:20]}",
                        "t": tenant_id,
                        "r": run_id,
                        "nm": t.name,
                        "pg": t.page,
                        "html": t.structure_html,
                        "rows": json.dumps(rows_json, ensure_ascii=False),
                        "cf": t.confidence,
                    },
                )
            # fallback_pages（VL 露出用, §5.4）は metrics JSONB へマージ（既存キーは保持）。
            c.execute(
                text(
                    "UPDATE extraction_runs SET status = :st, finished_at = now(), "
                    "metrics = COALESCE(metrics, '{}'::jsonb) || CAST(:m AS jsonb) WHERE id = :r"
                ),
                {
                    "st": status,
                    "r": run_id,
                    "m": json.dumps({"fallback_pages": sorted(set(fallback_pages or []))}),
                },
            )
            c.execute(
                text(
                    "UPDATE documents SET status = :st, updated_at = now() "
                    "WHERE id = (SELECT document_id FROM extraction_runs WHERE id = :r)"
                ),
                {"st": status, "r": run_id},
            )

    def add_run_metrics(self, tenant_id, run_id, patch) -> None:
        # 加算で追記（resume ジョブでも積み上がる）。metrics 全体の上書きはしない
        from sqlalchemy import text

        sets = []
        params: dict = {"t": tenant_id, "r": run_id}
        for i, (k, v) in enumerate(patch.items()):
            if not k.replace("_", "").isalnum():
                continue  # 識別子でないキーは無視（SQL へ埋めるため）
            sets.append(
                f"'{k}', COALESCE((metrics->>'{k}')::numeric, 0) + :v{i}"
            )
            params[f"v{i}"] = v
        if not sets:
            return
        with self._engine.begin() as c:
            c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
            c.execute(
                text(
                    "UPDATE extraction_runs SET metrics = metrics ||"
                    f" jsonb_build_object({', '.join(sets)})"
                    " WHERE tenant_id=:t AND id=:r"
                ),
                params,
            )

    def set_job_status(self, tenant_id, job_id, status, *, error_code=None) -> None:
        # §6.3 の契約はクライアントが GET /jobs/{id} を polling して終了を待つこと。
        # ここを書かないと jobs.status は queued のままで、ジョブが成功しても
        # クライアントはタイムアウトするまで待ち続ける。
        with self._engine.begin() as c:
            c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
            c.execute(
                text(
                    "UPDATE jobs SET status = :st, error_code = :ec, "
                    " started_at = COALESCE(started_at, CASE WHEN :st = 'running' "
                    "                       THEN now() ELSE started_at END), "
                    " finished_at = CASE WHEN :st IN ('succeeded','failed','dead') "
                    "                    THEN now() ELSE finished_at END, "
                    " attempt = attempt + CASE WHEN :st = 'running' THEN 1 ELSE 0 END "
                    "WHERE id = :j AND tenant_id = :t"
                ),
                {"st": status, "ec": error_code, "j": job_id, "t": tenant_id},
            )
