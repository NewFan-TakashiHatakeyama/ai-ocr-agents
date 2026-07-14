# mypy: ignore-errors
"""確定 Run を PostgreSQL から読み ExportInput を組む（§5.9 / §7）。runtime 依存（sqlalchemy+psycopg）。

配信先 webhook は connections（type='webhook', status in active/tested）の config から解決する。
RLS 用に set_config('app.tenant_id', ...) を発行（§7.3）。
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import create_engine, text

from newfan_export.models import ExportInput, WebhookEndpoint
from newfan_schemas import ExtractedField, TableResult


class PgExportSource:
    def __init__(self, dsn: str) -> None:
        self._engine = create_engine(dsn, future=True)

    def _rls(self, c, tenant_id: str) -> None:
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})

    def load_export_input(self, tenant_id: str, run_id: str) -> Optional[ExportInput]:
        with self._engine.begin() as c:
            self._rls(c, tenant_id)
            run = c.execute(
                text(
                    "SELECT document_id, status, engine_versions FROM extraction_runs "
                    "WHERE id=:r AND tenant_id=:t"
                ),
                {"r": run_id, "t": tenant_id},
            ).first()
            if run is None:
                return None
            document_id, status, engine_versions = run
            field_rows = c.execute(
                text(
                    "SELECT field_name, value_raw, value_normalized, final_value, confidence, "
                    " grounding_score, page_no, bbox, source_quote, span_ids, correction, "
                    " validation, review_status FROM extraction_fields "
                    "WHERE run_id=:r ORDER BY field_name"
                ),
                {"r": run_id},
            ).all()
            table_rows = c.execute(
                text(
                    "SELECT name, page_no, structure_html, rows, confidence "
                    "FROM extraction_tables WHERE run_id=:r ORDER BY name"
                ),
                {"r": run_id},
            ).all()

        fields = [
            ExtractedField.model_validate(
                {
                    "name": r.field_name,
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
            for r in field_rows
        ]
        tables = [
            TableResult.model_validate(
                {
                    "name": r.name,
                    "page": r.page_no,
                    "structure_html": r.structure_html,
                    "rows": r.rows or [],
                    "confidence": r.confidence,
                }
            )
            for r in table_rows
        ]
        return ExportInput(
            tenant_id=tenant_id,
            document_id=document_id,
            run_id=run_id,
            status=status,
            engine_versions=engine_versions or {},
            fields=fields,
            tables=tables,
        )

    def list_webhook_endpoints(self, tenant_id: str) -> list[WebhookEndpoint]:
        with self._engine.begin() as c:
            self._rls(c, tenant_id)
            rows = c.execute(
                text(
                    "SELECT config FROM connections WHERE tenant_id=:t AND type='webhook' "
                    "AND status IN ('active','tested')"
                ),
                {"t": tenant_id},
            ).all()
        endpoints: list[WebhookEndpoint] = []
        for (config,) in rows:
            url = (config or {}).get("url")
            if url:
                endpoints.append(WebhookEndpoint(url=url, secret=(config or {}).get("secret", "")))
        return endpoints
