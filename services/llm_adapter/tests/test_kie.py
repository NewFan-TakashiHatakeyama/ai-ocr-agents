import json

from newfan_schemas import Span

from newfan_llm_adapter import FakeProvider, LLMAdapter, PromptBundle, kie_extract

_SPANS = [
    Span(span_id=10, page=1, text="御請求金額", conf=0.99, bbox=[0, 0, 1, 1]),
    Span(span_id=11, page=1, text="¥128,000", conf=0.72, bbox=[0, 0, 1, 1]),
]

_SCHEMA = {"doc_type": "invoice", "fields": [{"name": "total_amount", "type": "money_jpy"}]}


def _kie_response(fields: list[dict], tables: list[dict] | None = None) -> str:
    return json.dumps(
        {"fields": fields, "tables": tables or [], "unmapped_required": []},
        ensure_ascii=False,
    )


def test_kie_maps_fields_with_valid_spans(bundle: PromptBundle) -> None:
    resp = _kie_response(
        [{"name": "total_amount", "value": "128000", "span_ids": [11], "page": 1}]
    )
    adapter = LLMAdapter(FakeProvider([resp]))
    result = kie_extract(
        adapter, bundle, spans=_SPANS, layout_markdown="# 請求書", schema_json=_SCHEMA
    )
    assert len(result.fields) == 1
    f = result.fields[0]
    assert f.name == "total_amount"
    assert f.value_raw == "128000"
    assert f.span_ids == [11]
    assert f.source_quote == "¥128,000"  # 実在 span テキスト


def test_kie_drops_hallucinated_span_ids(bundle: PromptBundle) -> None:
    # LLM が存在しない span_id=999 を返す → 検証で除去、source_quote なし
    resp = _kie_response(
        [{"name": "total_amount", "value": "999999", "span_ids": [999], "page": 1}]
    )
    adapter = LLMAdapter(FakeProvider([resp]))
    result = kie_extract(
        adapter, bundle, spans=_SPANS, layout_markdown="", schema_json=_SCHEMA
    )
    f = result.fields[0]
    assert f.span_ids == []  # 捏造 span を除去
    assert f.source_quote is None  # 根拠なし → 後段で grounding 0 → レビュー


def test_kie_tables(bundle: PromptBundle) -> None:
    resp = _kie_response(
        [],
        tables=[
            {
                "name": "line_items",
                "rows": [{"cells": {"item": {"value": "A", "span_ids": [10]}}}],
            }
        ],
    )
    adapter = LLMAdapter(FakeProvider([resp]))
    result = kie_extract(
        adapter, bundle, spans=_SPANS, layout_markdown="", schema_json=_SCHEMA
    )
    assert result.tables[0].name == "line_items"
    assert result.tables[0].rows[0]["item"].value == "A"
    assert result.tables[0].rows[0]["item"].span_ids == [10]
