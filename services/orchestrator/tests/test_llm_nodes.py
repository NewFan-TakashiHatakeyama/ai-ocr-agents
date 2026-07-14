"""kie_extract/llm_correct ノードが llm-adapter で実体化されることの検証（FakeProvider）。"""

import json

from newfan_llm_adapter import FakeProvider, LLMAdapter, PromptBundle, default_bundle_dir
from newfan_schemas import ExtractedField, ReviewStatus, Span

from newfan_orchestrator import llm_nodes

_BUNDLE = PromptBundle.load(default_bundle_dir())
_SCHEMA = {
    "doc_type": "invoice",
    "fields": [{"name": "total_amount", "type": "money_jpy", "critical": True}],
}


def test_kie_node_populates_fields() -> None:
    spans = [Span(span_id=11, page=1, text="¥128,000", conf=0.72, bbox=[0, 0, 1, 1])]
    resp = json.dumps(
        {
            "fields": [{"name": "total_amount", "value": "128000", "span_ids": [11], "page": 1}],
            "tables": [],
            "unmapped_required": [],
        }
    )
    adapter = LLMAdapter(FakeProvider([resp]))
    node = llm_nodes.make_kie_extract(adapter, _BUNDLE)
    out = node({"spans": spans, "layout_markdown": "# 請求書", "schema": _SCHEMA})
    assert out["fields"][0].name == "total_amount"
    assert out["fields"][0].span_ids == [11]


def test_correct_node_applies_confusion_pair() -> None:
    # 低確信フィールド。O→0 は混同文字表にある → 自動適用
    span = Span(span_id=1, page=1, text="128,OOO", conf=0.6, bbox=[0, 0, 1, 1])
    field = ExtractedField(
        name="total_amount", value_raw="128,OOO", confidence=0.6, span_ids=[1]
    )
    resp = json.dumps(
        {
            "corrected": "128000",
            "changed": True,
            "needs_review": False,
            "used_pairs": [["O", "0"]],
            "memory_refs": [],
            "rationale": "視覚的に O は 0",
            "confidence": 0.93,
        }
    )
    adapter = LLMAdapter(FakeProvider([resp]))
    node = llm_nodes.make_llm_correct(adapter, _BUNDLE)
    out = node({"fields": [field], "spans": [span], "schema": _SCHEMA})
    f = out["fields"][0]
    assert f.value_normalized == "128000"
    assert f.correction is not None and f.correction["applied"] is True


def test_correct_node_blocks_disallowed_pair() -> None:
    span = Span(span_id=1, page=1, text="128000", conf=0.6, bbox=[0, 0, 1, 1])
    field = ExtractedField(name="total_amount", value_raw="128000", confidence=0.6, span_ids=[1])
    # 1→9 は混同文字表に無い → DD-10 違反 → 適用せず review
    resp = json.dumps(
        {
            "corrected": "198000",
            "changed": True,
            "needs_review": False,
            "used_pairs": [["1", "9"]],
            "memory_refs": [],
            "rationale": "",
            "confidence": 0.9,
        }
    )
    adapter = LLMAdapter(FakeProvider([resp]))
    node = llm_nodes.make_llm_correct(adapter, _BUNDLE)
    out = node({"fields": [field], "spans": [span], "schema": _SCHEMA})
    f = out["fields"][0]
    assert f.value_normalized != "198000"  # 適用されていない
    assert f.review_status is ReviewStatus.PENDING


def test_correct_node_skips_high_confidence() -> None:
    field = ExtractedField(name="total_amount", value_raw="128000", confidence=0.95, span_ids=[1])
    adapter = LLMAdapter(FakeProvider([]))  # 呼ばれないはず
    node = llm_nodes.make_llm_correct(adapter, _BUNDLE)
    out = node({"fields": [field], "spans": [], "schema": _SCHEMA})
    assert out["fields"][0].correction is None
