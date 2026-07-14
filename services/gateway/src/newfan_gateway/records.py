"""リポジトリのレコード型（DB 行のアプリ表現。§7 DDL に対応）。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field

from newfan_schemas import ExtractedField, TableResult


def _now() -> datetime:
    return datetime.now(timezone.utc)


class DocumentRecord(BaseModel):
    id: str
    tenant_id: str
    storage_uri: str
    original_name: Optional[str] = None
    mime_type: str
    page_count: Optional[int] = None
    doc_type: Optional[str] = None
    external_ref: Optional[str] = None
    status: str = "uploaded"
    created_at: datetime = Field(default_factory=_now)


class PageRecord(BaseModel):
    page_no: int
    width: Optional[int] = None
    height: Optional[int] = None
    image_uri: str
    preproc: dict[str, Any] = Field(default_factory=dict)


class RunRecord(BaseModel):
    id: str
    tenant_id: str
    document_id: str
    schema_id: Optional[str] = None
    status: str = "processing"
    engine_versions: dict[str, Any] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)
    result_version: int = 1
    fields: list[ExtractedField] = Field(default_factory=list)
    tables: list[TableResult] = Field(default_factory=list)
    review_summary: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime = Field(default_factory=_now)


class JobRecord(BaseModel):
    id: str
    tenant_id: str
    kind: str
    ref_id: str
    status: str = "queued"
    error_code: Optional[str] = None


class CorrectionRecord(BaseModel):
    id: str
    tenant_id: str
    document_id: str
    run_id: str
    field_name: str
    original_value: Optional[str] = None
    corrected_value: str
    note: Optional[str] = None
    created_at: datetime = Field(default_factory=_now)
