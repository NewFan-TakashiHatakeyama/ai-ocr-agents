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
    # VL フォールバックしたページ番号（品質ゲート NG, §5.4/DD-09）。UI のバッジ/バナー用。
    fallback_pages: list[int] = Field(default_factory=list)
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


# ---- 管理画面（SCR-04/05/06） ----


class SchemaFieldDef(BaseModel):
    name: str
    label: Optional[str] = None
    type: str = "string"
    required: bool = False
    critical: bool = False
    columns: Optional[list[dict[str, Any]]] = None  # table 型の列定義（§5.5）


class SchemaRecord(BaseModel):
    """field_schemas 行（§7.2）。有効版＝doc_type ごとの最新 version。"""

    id: str
    tenant_id: str
    doc_type: str
    version: int
    fields: list[SchemaFieldDef] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=_now)


class RuleRecord(BaseModel):
    """tenant_rules 行（§5.8.4）。"""

    id: str
    tenant_id: str
    doc_type: Optional[str] = None
    supplier_key: Optional[str] = None
    field_name: Optional[str] = None
    rule_type: str
    rule_json: dict[str, Any] = Field(default_factory=dict)
    status: str = "draft"
    validation_report: Optional[dict[str, Any]] = None
    source_correction_ids: list[str] = Field(default_factory=list)
    created_by: str = "agent"
    updated_at: datetime = Field(default_factory=_now)


class MetricsSummary(BaseModel):
    """ダッシュボード KPI（§12.1 と対応。未計測項目は None）。"""

    total_documents: int = 0
    status_counts: dict[str, int] = Field(default_factory=dict)
    stp_rate: float = 0.0
    corrections_total: int = 0
    active_rules: int = 0
    pending_rules: int = 0
    memories_total: int = 0
    field_accuracy_sampled: Optional[float] = None  # 週次サンプル監査（データ源未整備）
    llm_cost_jpy_total: Optional[float] = None  # トークン計測未整備
