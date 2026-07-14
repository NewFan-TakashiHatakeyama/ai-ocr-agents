"""レコード型（§7.2 correction_logs / tenant_memories / tenant_rules）。"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(timezone.utc)


class RuleType(str, Enum):
    REGEX_REPLACE = "regex_replace"
    VOCAB_MAP = "vocab_map"
    FORMAT = "format"
    CHECKSUM = "checksum"
    LLM_HINT = "llm_hint"


class RuleStatus(str, Enum):
    DRAFT = "draft"
    VALIDATING = "validating"
    ACTIVE = "active"
    RETIRED = "retired"


class CorrectionLog(BaseModel):
    id: str
    tenant_id: str
    document_id: str
    run_id: str
    field_name: str
    original_value: Optional[str] = None
    corrected_value: str
    doc_type: Optional[str] = None
    supplier_key: Optional[str] = None
    context: Optional[str] = None
    reviewer_id: Optional[str] = None
    embedded: bool = False
    created_at: datetime = Field(default_factory=_now)


class TenantMemory(BaseModel):
    id: str
    tenant_id: str
    correction_log_id: str
    faiss_vector_id: int
    embed_model: str
    created_at: datetime = Field(default_factory=_now)


class TenantRule(BaseModel):
    id: str
    tenant_id: str
    doc_type: Optional[str] = None
    supplier_key: Optional[str] = None
    field_name: Optional[str] = None
    rule_type: RuleType
    rule_json: dict[str, Any] = Field(default_factory=dict)
    status: RuleStatus = RuleStatus.DRAFT
    validation_report: Optional[dict[str, Any]] = None
    source_correction_ids: list[str] = Field(default_factory=list)
    created_by: str = "agent"
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
