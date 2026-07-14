"""ワーカー統合テスト（§9 / §4.4）: extract 自動確定 / needs_review interrupt / resume。

外部境界（GPU/LLM）は Fake、checkpointer は MemorySaver。ContextStore/Consumer は InMemory。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

pytest.importorskip("langgraph.checkpoint.memory")

from langgraph.checkpoint.memory import MemorySaver

from newfan_llm_adapter import FakeProvider, LLMAdapter, PromptBundle, default_bundle_dir
from newfan_memory import HashingEmbedder, InMemoryMemoryRepository, MemoryService
from newfan_paddle_client import LayoutParsingResponse

from newfan_orchestrator.consumer import InMemoryQueueConsumer
from newfan_orchestrator.graph import build_graph
from newfan_orchestrator.persistence import InMemoryContextStore
from newfan_orchestrator.serde import newfan_serde
from newfan_orchestrator.worker import ExtractionWorker

_BUNDLE = PromptBundle.load(default_bundle_dir())
_SCHEMA = {
    "doc_type": "invoice",
    "fields": [{"name": "total_amount", "label": "合計金額(税込)", "type": "money_jpy", "critical": True}],
}


def _layout(conf: float) -> dict[str, Any]:
    return {
        "layoutParsingResults": [
            {
                "prunedResult": {
                    "parsing_res_list": [
                        {"block_bbox": [40, 40, 520, 90], "block_label": "text", "block_content": "x", "block_id": 0, "block_order": 0}
                    ],
                    "overall_ocr_res": {
                        "rec_texts": ["128000"],
                        "rec_scores": [conf],
                        "rec_polys": [[[300, 180], [430, 180], [430, 212], [300, 212]]],
                    },
                },
                "markdown": {"text": "md", "isStart": True, "isEnd": True},
            }
        ]
    }


class _FakeStructure:
    def __init__(self, conf: float) -> None:
        self._conf = conf

    def layout_parsing(self, file_b64: str, *, file_type: int = 1) -> LayoutParsingResponse:
        return LayoutParsingResponse.model_validate(_layout(self._conf))


def _llm_handler(system: str, user: str) -> str:
    if "校正" in system:  # llm_correct: 変更しない（DD-10 未適合扱い）
        return json.dumps({"corrected": "128000", "changed": False, "needs_review": True, "used_pairs": [], "memory_refs": [], "rationale": "", "confidence": 0.5})
    return json.dumps({"fields": [{"name": "total_amount", "value": "128000", "span_ids": [0], "page": 1}], "tables": [], "unmapped_required": []})


def _build(conf: float) -> tuple[ExtractionWorker, InMemoryContextStore, list, list]:
    store = InMemoryContextStore()
    store.seed_run(
        "run_1",
        tenant_id="ten_1",
        document_id="doc_1",
        schema=_SCHEMA,
        pages=[{"page_no": 1, "image_uri": "x"}],
    )
    exports: list = []
    webhooks: list = []
    graph = build_graph(
        checkpointer=MemorySaver(serde=newfan_serde()),
        adapter=LLMAdapter(FakeProvider(handler=_llm_handler)),
        bundle=_BUNDLE,
        memory=MemoryService(HashingEmbedder(), InMemoryMemoryRepository()),
        structure_client=_FakeStructure(conf),
        image_loader=lambda uri: b"png",
        context_store=store,
        export_enqueue=lambda stream, msg: exports.append((stream, msg)),
    )
    consumer = InMemoryQueueConsumer()
    worker = ExtractionWorker(graph, store, consumer, webhook=lambda ev, d: webhooks.append((ev, d)))
    return worker, store, exports, webhooks


def test_extract_auto_confirm() -> None:
    worker, store, exports, _ = _build(conf=0.99)
    status = worker.process({"run_id": "run_1", "tenant_id": "ten_1"})
    assert status == "confirmed"
    assert store.run_status("run_1") == "confirmed"
    assert store.document_status("doc_1") == "confirmed"
    # finalize が保存＋ q.export enqueue
    assert store.saved_fields("run_1")[0].value_normalized == "128000"
    assert exports and exports[0][0] == "q.export"


def test_extract_needs_review_interrupts() -> None:
    worker, store, exports, webhooks = _build(conf=0.78)
    status = worker.process({"run_id": "run_1", "tenant_id": "ten_1"})
    assert status == "needs_review"
    assert store.run_status("run_1") == "needs_review"
    # interrupt 停止時点の fields が保存され、webhook 発火・export はまだ
    assert store.saved_fields("run_1")
    assert webhooks and webhooks[0][0] == "document.needs_review"
    assert exports == []


def test_resume_after_review_confirms() -> None:
    worker, store, exports, _ = _build(conf=0.78)
    assert worker.process({"run_id": "run_1", "tenant_id": "ten_1"}) == "needs_review"
    # レビュアが total_amount を修正して確定 → resume
    feedback = {"corrections": [{"field_name": "total_amount", "corrected_value": "178000"}]}
    status = worker.process({"run_id": "run_1", "tenant_id": "ten_1", "resume": feedback})
    assert status == "confirmed"
    assert store.run_status("run_1") == "confirmed"
    saved = {f.name: f.value_normalized for f in store.saved_fields("run_1")}
    assert saved["total_amount"] == "178000"  # apply_feedback で確定値反映
    assert exports and exports[0][0] == "q.export"


def test_fallback_pages_persisted() -> None:
    # ページ平均 conf < 0.75 → quality_gate が fallback_pages=[1]（§5.4）。
    worker, store, _, _ = _build(conf=0.70)
    assert worker.process({"run_id": "run_1", "tenant_id": "ten_1"}) == "needs_review"
    assert store.saved_fallback_pages("run_1") == [1]  # needs_review 保存に露出
    # レビュア修正で resume→確定してもページ露出は維持される
    feedback = {"corrections": [{"field_name": "total_amount", "corrected_value": "128000"}]}
    worker.process({"run_id": "run_1", "tenant_id": "ten_1", "resume": feedback})
    assert store.run_status("run_1") == "confirmed"
    assert store.saved_fallback_pages("run_1") == [1]


def test_no_fallback_when_quality_ok() -> None:
    worker, store, _, _ = _build(conf=0.99)
    worker.process({"run_id": "run_1", "tenant_id": "ten_1"})
    assert store.saved_fallback_pages("run_1") == []


def test_worker_run_once_consumes_queue() -> None:
    worker, store, _, _ = _build(conf=0.99)
    worker._consumer.push({"run_id": "run_1", "tenant_id": "ten_1"})  # type: ignore[attr-defined]
    assert worker._consumer.pending_count() == 1  # type: ignore[attr-defined]
    processed = worker.run_once()
    assert processed == 1
    assert worker._consumer.pending_count() == 0  # type: ignore[attr-defined]
    assert store.run_status("run_1") == "confirmed"
