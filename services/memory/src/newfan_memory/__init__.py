"""memory-svc: 修正メモリ/ルール（§5.8, DD-06/DD-07）。"""

from newfan_memory.embedding import (
    EMBED_DIM,
    Embedder,
    HashingEmbedder,
    embedding_key,
    passage_text,
    query_text,
)
from newfan_memory.index import InMemoryIndex, VectorIndex
from newfan_memory.records import (
    CorrectionLog,
    RuleStatus,
    RuleType,
    TenantMemory,
    TenantRule,
)
from newfan_memory.repository import InMemoryMemoryRepository, MemoryRepository
from newfan_memory.rule_extract import extract_rules
from newfan_memory.rules import ValidationReport, apply_rule, finalize_status, validate_rule
from newfan_memory.service import LearnResult, MemoryService

__all__ = [
    "EMBED_DIM",
    "Embedder",
    "HashingEmbedder",
    "embedding_key",
    "query_text",
    "passage_text",
    "VectorIndex",
    "InMemoryIndex",
    "CorrectionLog",
    "TenantMemory",
    "TenantRule",
    "RuleType",
    "RuleStatus",
    "MemoryRepository",
    "InMemoryMemoryRepository",
    "apply_rule",
    "validate_rule",
    "finalize_status",
    "ValidationReport",
    "extract_rules",
    "MemoryService",
    "LearnResult",
]
