"""API リクエスト/レスポンス DTO（§6.3）。"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from newfan_schemas import ExtractedField, TableResult


class DocumentCreated(BaseModel):
    document_id: str
    page_count: Optional[int]
    status: str


class DocumentMeta(BaseModel):
    document_id: str
    status: str
    doc_type: Optional[str] = None
    external_ref: Optional[str] = None
    page_count: Optional[int] = None


class DocumentList(BaseModel):
    items: list[DocumentMeta]
    next_cursor: Optional[str] = None


class ExtractOptions(BaseModel):
    force_vl: bool = False
    two_model_check: bool = False
    language: str = "ja"


class ExtractRequest(BaseModel):
    schema_id: Optional[str] = None
    options: ExtractOptions = Field(default_factory=ExtractOptions)


class ExtractAccepted(BaseModel):
    job_id: str
    run_id: str


class JobStatus(BaseModel):
    job_id: str
    kind: str
    status: str
    error_code: Optional[str] = None


class ResultResponse(BaseModel):
    document_id: str
    run_id: str
    status: str
    result_version: int
    engine_versions: dict[str, Any]
    fields: list[ExtractedField]
    tables: list[TableResult]
    review_summary: dict[str, Any]


class CorrectionItem(BaseModel):
    field_name: str
    original_value: Optional[str] = None
    corrected_value: str
    note: Optional[str] = None


class CorrectionsRequest(BaseModel):
    run_id: str
    items: list[CorrectionItem]
    version: int


class CorrectionsAccepted(BaseModel):
    correction_ids: list[str]


class ConfirmRequest(BaseModel):
    run_id: Optional[str] = None
    overrides: Optional[dict[str, Any]] = None


class ConfirmAccepted(BaseModel):
    status: str = "accepted"


class ReviewQueueItem(BaseModel):
    document_id: str
    run_id: str
    pending: int
    priority: float


class ReviewQueue(BaseModel):
    items: list[ReviewQueueItem]


class SignedUrl(BaseModel):
    url: str
    expires_in: int


# ---- 管理画面 DTO（SCR-04/05/06） ----


class SchemaFieldDto(BaseModel):
    name: str
    label: Optional[str] = None
    type: str = "string"
    required: bool = False
    critical: bool = False
    columns: Optional[list[dict[str, Any]]] = None


class SchemaDto(BaseModel):
    doc_type: str
    version: int
    fields: list[SchemaFieldDto]


class SchemaList(BaseModel):
    items: list[SchemaDto]


class PutSchemaRequest(BaseModel):
    doc_type: str
    fields: list[SchemaFieldDto]


class RuleDto(BaseModel):
    id: str
    doc_type: Optional[str] = None
    supplier_key: Optional[str] = None
    field_name: Optional[str] = None
    rule_type: str
    rule_json: dict[str, Any]
    status: str
    validation_report: Optional[dict[str, Any]] = None
    source_correction_ids: list[str] = Field(default_factory=list)
    created_by: str = "agent"
    activatable: bool = False


class RuleList(BaseModel):
    items: list[RuleDto]


class PatchRuleRequest(BaseModel):
    status: str  # "active"（有効化）/ "retired"（退役）


class MetricsResponse(BaseModel):
    total_documents: int
    status_counts: dict[str, int]
    stp_rate: float
    corrections_total: int
    active_rules: int
    pending_rules: int
    memories_total: int
    field_accuracy_sampled: Optional[float] = None
    llm_cost_jpy_total: Optional[float] = None
