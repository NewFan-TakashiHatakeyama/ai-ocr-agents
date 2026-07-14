"""エクスポート入力（確定 Run の最小表現）。"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from newfan_schemas import ExtractedField, TableResult


class ExportInput(BaseModel):
    tenant_id: str
    document_id: str
    run_id: str
    status: str = "confirmed"
    engine_versions: dict[str, Any] = Field(default_factory=dict)
    fields: list[ExtractedField] = Field(default_factory=list)
    tables: list[TableResult] = Field(default_factory=list)
    review_summary: dict[str, Any] = Field(default_factory=dict)
    external_ref: Optional[str] = None


class WebhookEndpoint(BaseModel):
    url: str
    secret: str
