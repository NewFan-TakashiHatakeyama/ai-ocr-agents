"""MemoryService: 検索/登録/アクティブルール/learn（§5.8）。

learn は修正を embedding 登録し、同種修正が min_evidence 以上たまったらルール抽出→自動検証→
draft/active 保存（§5.8.4）。テナント別 index を保持（DD-07）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from newfan_llm_adapter import LLMAdapter, PromptBundle

from newfan_memory.embedding import Embedder, embedding_key, passage_text, query_text
from newfan_memory.ids import new_id
from newfan_memory.index import InMemoryIndex, VectorIndex
from newfan_memory.records import (
    CorrectionLog,
    RuleStatus,
    RuleType,
    TenantMemory,
    TenantRule,
)
from newfan_memory.repository import MemoryRepository
from newfan_memory.rule_extract import extract_rules
from newfan_memory.rules import finalize_status, validate_rule

# §2.5 初期値
KNN_TOPK = 5
SIM_THRESHOLD = 0.75
MIN_EVIDENCE = 5

_RULE_META_KEYS = {"rule_type", "evidence_ids", "evidence", "condition"}


@dataclass
class LearnResult:
    correction_id: str
    rule_extraction_triggered: bool = False
    new_rules: list[TenantRule] = field(default_factory=list)


class MemoryService:
    def __init__(
        self,
        embedder: Embedder,
        repo: MemoryRepository,
        *,
        index_factory: Callable[[], VectorIndex] = InMemoryIndex,
        adapter: Optional[LLMAdapter] = None,
        bundle: Optional[PromptBundle] = None,
        knn_topk: int = KNN_TOPK,
        sim_threshold: float = SIM_THRESHOLD,
        min_evidence: int = MIN_EVIDENCE,
        embed_model_name: str = "intfloat/multilingual-e5-small",
    ) -> None:
        self._embedder = embedder
        self._repo = repo
        self._index_factory = index_factory
        self._adapter = adapter
        self._bundle = bundle
        self._knn_topk = knn_topk
        self._sim_threshold = sim_threshold
        self._min_evidence = min_evidence
        self._embed_model_name = embed_model_name
        self._indices: dict[str, VectorIndex] = {}

    def _index(self, tenant_id: str) -> VectorIndex:
        idx = self._indices.get(tenant_id)
        if idx is None:
            idx = self._index_factory()
            self._indices[tenant_id] = idx
        return idx

    # --- 検索（§5.8.1 /internal/memory/search） ---
    def search(
        self,
        *,
        tenant_id: str,
        doc_type: str,
        supplier: str,
        field_name: str,
        value_raw: str,
        context: str,
    ) -> list[dict[str, Any]]:
        key = embedding_key(
            doc_type=doc_type,
            supplier=supplier,
            field=field_name,
            value_raw=value_raw,
            context=context,
        )
        qvec = self._embedder.embed(query_text(key))
        hits = self._index(tenant_id).search(qvec, self._knn_topk)
        examples: list[dict[str, Any]] = []
        for vector_id, score in hits:
            if score < self._sim_threshold:
                continue
            mem = self._repo.get_memory_by_vector(tenant_id, vector_id)
            if mem is None:
                continue
            c = self._repo.get_correction(tenant_id, mem.correction_log_id)
            if c is None:
                continue
            examples.append(
                {
                    "doc_type": c.doc_type,
                    "supplier": c.supplier_key,
                    "field": c.field_name,
                    "from": c.original_value,
                    "to": c.corrected_value,
                    "note": c.context,
                    "score": round(score, 4),
                }
            )
        return examples

    # --- アクティブルール取得（§5.8.1 /internal/rules/active） ---
    def active_rules(self, tenant_id: str, doc_type: Optional[str] = None) -> list[TenantRule]:
        return self._repo.list_rules(tenant_id, doc_type=doc_type, status=RuleStatus.ACTIVE)

    # --- 登録（§5.8.1 /internal/memory/add） ---
    def add(self, log: CorrectionLog) -> None:
        self._repo.add_correction(log)  # 正本を保存（冪等）
        key = embedding_key(
            doc_type=log.doc_type or "",
            supplier=log.supplier_key or "",
            field=log.field_name,
            value_raw=log.original_value or "",
            context=log.context or "",
        )
        pvec = self._embedder.embed(passage_text(key))
        vector_id = self._repo.next_vector_id(log.tenant_id)
        self._index(log.tenant_id).add(vector_id, pvec)
        self._repo.add_memory(
            TenantMemory(
                id=new_id("memory"),
                tenant_id=log.tenant_id,
                correction_log_id=log.id,
                faiss_vector_id=vector_id,
                embed_model=self._embed_model_name,
            )
        )

    # --- learn（§4.3 learn ノード）: 登録 + ルール抽出トリガ ---
    def learn(
        self,
        *,
        tenant_id: str,
        document_id: str,
        run_id: str,
        field_name: str,
        original_value: Optional[str],
        corrected_value: str,
        doc_type: Optional[str] = None,
        supplier_key: Optional[str] = None,
        context: Optional[str] = None,
        reviewer_id: Optional[str] = None,
    ) -> LearnResult:
        log = CorrectionLog(
            id=new_id("correction"),
            tenant_id=tenant_id,
            document_id=document_id,
            run_id=run_id,
            field_name=field_name,
            original_value=original_value,
            corrected_value=corrected_value,
            doc_type=doc_type,
            supplier_key=supplier_key,
            context=context,
            reviewer_id=reviewer_id,
        )
        self.add(log)  # 正本保存 + embedding 登録（§5.8.1）

        result = LearnResult(correction_id=log.id)
        if self._adapter is None or self._bundle is None:
            return result

        # 同種修正（tenant, doc_type, field）が閾値以上ならルール抽出（§5.8.4）
        if self._repo.count_corrections(tenant_id, doc_type, field_name) >= self._min_evidence:
            result.rule_extraction_triggered = True
            result.new_rules = self._extract_and_validate(tenant_id, doc_type, field_name)
        return result

    def _to_rule(self, tenant_id: str, item: dict[str, Any]) -> TenantRule:
        rule_json = {k: v for k, v in item.items() if k not in _RULE_META_KEYS}
        return TenantRule(
            id=new_id("rule"),
            tenant_id=tenant_id,
            doc_type=item.get("doc_type"),
            supplier_key=item.get("supplier"),
            field_name=item.get("field"),
            rule_type=RuleType(item["rule_type"]),
            rule_json=rule_json,
            source_correction_ids=[str(x) for x in (item.get("evidence_ids") or [])],
            created_by="agent",
        )

    def _extract_and_validate(
        self, tenant_id: str, doc_type: Optional[str], field_name: str
    ) -> list[TenantRule]:
        assert self._adapter is not None and self._bundle is not None
        corrections = self._repo.list_corrections(
            tenant_id, doc_type=doc_type, field_name=field_name
        )
        drafts = extract_rules(self._adapter, self._bundle, corrections)
        holdout = [(c.original_value or "", c.corrected_value) for c in corrections]
        confirmed = [c.corrected_value for c in corrections]

        saved: list[TenantRule] = []
        for item in drafts:
            rule = self._to_rule(tenant_id, item)
            report = validate_rule(rule, holdout, confirmed)
            rule.validation_report = report.to_dict()
            rule.status = finalize_status(rule, report)
            self._repo.add_rule(rule)
            saved.append(rule)
        return saved
