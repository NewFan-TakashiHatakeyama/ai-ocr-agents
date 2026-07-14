"""抽出グラフの組み立て（詳細設計 §4.1 / §4.4）。

langgraph は optional-dependency（graph extra）。未インストール環境では build_graph が
明示エラーを出す。ノード/分岐ロジック（nodes.py）は langgraph 非依存で単体テスト可能。
"""

from __future__ import annotations

from typing import Any, Optional

from newfan_llm_adapter import LLMAdapter, PromptBundle
from newfan_memory import MemoryService
from newfan_schemas import ExtractionState

from newfan_orchestrator import llm_nodes, memory_nodes, nodes, ocr_nodes
from newfan_orchestrator.ocr_nodes import ImageLoader, StructureClient, file_uri_loader


def build_graph(
    checkpointer: Any | None = None,
    *,
    adapter: Optional[LLMAdapter] = None,
    bundle: Optional[PromptBundle] = None,
    memory: Optional[MemoryService] = None,
    structure_client: Optional[StructureClient] = None,
    image_loader: ImageLoader = file_uri_loader,
) -> Any:
    """§4.1 のグラフを構築して compile 済みグラフを返す。

    thread_id = run_id で interrupt/resume（§4.4）。checkpointer は PostgresSaver を想定。
    adapter/bundle → kie_extract/llm_correct、memory → memory_lookup/learn、
    structure_client → structure_ocr が実体化される（未指定ならスタブ）。
    """
    kie_node = llm_nodes.make_kie_extract(adapter, bundle) if (adapter and bundle) else nodes.kie_extract
    correct_node = (
        llm_nodes.make_llm_correct(adapter, bundle) if (adapter and bundle) else nodes.llm_correct
    )
    memory_lookup_node = memory_nodes.make_memory_lookup(memory) if memory else nodes.memory_lookup
    learn_node = memory_nodes.make_learn(memory) if memory else nodes.learn
    structure_node = (
        ocr_nodes.make_structure_ocr(structure_client, image_loader)
        if structure_client
        else nodes.structure_ocr
    )
    try:
        from langgraph.graph import END, START, StateGraph
    except ModuleNotFoundError as exc:  # pragma: no cover - 実行環境依存
        raise RuntimeError(
            "langgraph が必要です: uv sync --extra graph（services/orchestrator[graph]）"
        ) from exc

    g: Any = StateGraph(ExtractionState)

    g.add_node("load_context", nodes.load_context)
    g.add_node("structure_ocr", structure_node)
    g.add_node("quality_gate", nodes.quality_gate)
    g.add_node("vl_fallback", nodes.vl_fallback)
    g.add_node("memory_lookup", memory_lookup_node)
    g.add_node("kie_extract", kie_node)
    g.add_node("deterministic_normalize", nodes.deterministic_normalize)
    g.add_node("confidence_score", nodes.confidence_score)
    g.add_node("llm_correct", correct_node)
    g.add_node("validate", nodes.validate)
    g.add_node("confidence_gate", nodes.confidence_gate_node)
    g.add_node("hitl_review", nodes.hitl_review)
    g.add_node("apply_feedback", nodes.apply_feedback)
    g.add_node("learn", learn_node)
    g.add_node("finalize", nodes.finalize)

    g.add_edge(START, "load_context")
    g.add_edge("load_context", "structure_ocr")
    g.add_edge("structure_ocr", "quality_gate")
    # G1: quality_gate が fallback_pages を確定 → 分岐
    g.add_conditional_edges(
        "quality_gate",
        nodes.route_quality_gate,
        {"vl_fallback": "vl_fallback", "memory_lookup": "memory_lookup"},
    )
    g.add_edge("vl_fallback", "memory_lookup")
    g.add_edge("memory_lookup", "kie_extract")
    g.add_edge("kie_extract", "deterministic_normalize")
    g.add_edge("deterministic_normalize", "confidence_score")
    g.add_edge("confidence_score", "llm_correct")
    g.add_edge("llm_correct", "validate")
    g.add_edge("validate", "confidence_gate")
    # G2 confidence_gate
    g.add_conditional_edges(
        "confidence_gate",
        nodes.route_confidence_gate,
        {"hitl_review": "hitl_review", "finalize": "finalize"},
    )
    g.add_edge("hitl_review", "apply_feedback")
    g.add_edge("apply_feedback", "learn")
    g.add_edge("learn", "finalize")
    g.add_edge("finalize", END)

    return g.compile(checkpointer=checkpointer)
