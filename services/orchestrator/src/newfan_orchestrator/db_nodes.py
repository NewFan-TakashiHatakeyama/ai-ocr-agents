"""load_context / finalize ノード（§4.3）を ContextStore で実体化する。

build_graph に context_store（＋ export_enqueue）を渡すと、スタブの代わりにこれらが使われる。
"""

from __future__ import annotations

from typing import Any, Callable

from newfan_schemas import ExtractionState

from newfan_orchestrator.persistence import ContextStore

NodeFn = Callable[[ExtractionState], dict[str, Any]]
# (stream, message) → None。q.export への enqueue（§9）。
EnqueueFn = Callable[[str, dict[str, Any]], None]


def make_load_context(store: ContextStore) -> NodeFn:
    def _node(state: ExtractionState) -> dict[str, Any]:
        ctx = store.load_context(state.get("tenant_id", ""), state.get("run_id", ""))
        if ctx is None:
            errors = list(state.get("errors", []))
            errors.append({"stage": "load_context", "code": "E2000", "error": "context not found"})
            return {"errors": errors}
        return {"document_id": ctx.document_id, "schema": ctx.schema, "pages": ctx.pages}

    return _node


def make_finalize(store: ContextStore, enqueue: EnqueueFn | None = None) -> NodeFn:
    def _node(state: ExtractionState) -> dict[str, Any]:
        tenant_id = state.get("tenant_id", "")
        run_id = state.get("run_id", "")
        # finalize に到達するのは自動確定 or HITL 確定後の経路のみ（§4.1 G2）→ status=confirmed
        store.save_result(
            tenant_id,
            run_id,
            fields=state.get("fields", []),
            tables=state.get("tables", []),
            review_items=[],
            status="confirmed",
            fallback_pages=state.get("fallback_pages", []),
        )
        if enqueue is not None:
            enqueue(
                "q.export",
                {
                    "run_id": run_id,
                    "tenant_id": tenant_id,
                    "document_id": state.get("document_id", ""),
                },
            )
        metrics = dict(state.get("metrics", {}))
        metrics["finalized"] = True
        return {"metrics": metrics}

    return _node
