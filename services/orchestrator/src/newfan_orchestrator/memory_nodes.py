"""memory 接続ノード（§4.3 memory_lookup / learn）を memory-svc で実体化する。

build_graph に memory を渡すと、スタブの代わりにこれらが使われる。
"""

from __future__ import annotations

from typing import Any, Callable

from newfan_memory import MemoryService
from newfan_schemas import ExtractionState

NodeFn = Callable[[ExtractionState], dict[str, Any]]


def make_memory_lookup(service: MemoryService) -> NodeFn:
    def _node(state: ExtractionState) -> dict[str, Any]:
        tenant_id = state.get("tenant_id", "")
        doc_type = dict(state.get("schema", {})).get("doc_type", "")
        rules = service.active_rules(tenant_id, doc_type)
        # doc レベルの few-shot（レイアウト文脈で類似修正を引く）
        examples = service.search(
            tenant_id=tenant_id,
            doc_type=doc_type,
            supplier="",
            field_name="",
            value_raw="",
            context=state.get("layout_markdown", "")[:200],
        )
        return {
            "memory_examples": examples,
            "active_rules": [r.model_dump(mode="json") for r in rules],
        }

    return _node


def make_learn(service: MemoryService) -> NodeFn:
    def _node(state: ExtractionState) -> dict[str, Any]:
        feedback = state.get("human_feedback") or {}
        tenant_id = state.get("tenant_id", "")
        doc_type = dict(state.get("schema", {})).get("doc_type")
        for corr in feedback.get("corrections", []) or []:
            service.learn(
                tenant_id=tenant_id,
                document_id=state.get("document_id", ""),
                run_id=state.get("run_id", ""),
                field_name=corr["field_name"],
                original_value=corr.get("original_value"),
                corrected_value=corr["corrected_value"],
                doc_type=doc_type,
                supplier_key=corr.get("supplier_key"),
                context=corr.get("context"),
            )
        return {}

    return _node
