from newfan_schemas import ExtractedField, Span, SpanSource

from newfan_orchestrator import nodes


def test_quality_gate_flags_low_mean_pages() -> None:
    spans = [
        Span(span_id=0, page=1, text="a", conf=0.95, bbox=[0, 0, 1, 1]),
        Span(span_id=1, page=1, text="b", conf=0.93, bbox=[0, 0, 1, 1]),
        Span(span_id=2, page=2, text="c", conf=0.50, bbox=[0, 0, 1, 1]),
        Span(span_id=3, page=2, text="d", conf=0.40, bbox=[0, 0, 1, 1]),
    ]
    out = nodes.quality_gate({"spans": spans})
    assert out["fallback_pages"] == [2]


def test_route_quality_gate() -> None:
    assert nodes.route_quality_gate({"fallback_pages": [2]}) == "vl_fallback"
    assert nodes.route_quality_gate({"fallback_pages": []}) == "memory_lookup"


def test_confidence_score_node() -> None:
    spans = [Span(span_id=42, page=1, text="128000", conf=0.9, bbox=[0, 0, 1, 1])]
    field = ExtractedField(
        name="total_amount",
        value_normalized="128000",
        source_quote="128000",
        span_ids=[42],
    )
    out = nodes.confidence_score({"spans": spans, "fields": [field]})
    f = out["fields"][0]
    assert f.grounding_score == 1.0
    assert f.confidence == 0.9  # min(ocr_conf=0.9, grounding=1.0)


def test_confidence_score_vl_source_capped() -> None:
    spans = [
        Span(span_id=1, page=1, text="x", conf=0.95, bbox=[0, 0, 1, 1], source=SpanSource.VL)
    ]
    field = ExtractedField(
        name="f", value_normalized="x", source_quote="x", span_ids=[1]
    )
    out = nodes.confidence_score({"spans": spans, "fields": [field]})
    # DD-09: VL 由来は grounding 上限 0.7 → confidence も 0.7
    assert out["fields"][0].confidence == 0.70


def test_confidence_gate_node_builds_review_items() -> None:
    field = ExtractedField(name="total_amount", confidence=0.5, grounding_score=1.0)
    schema = {
        "doc_type": "invoice",
        "fields": [{"name": "total_amount", "type": "money_jpy", "critical": True}],
    }
    out = nodes.confidence_gate_node({"fields": [field], "schema": schema})
    assert len(out["review_items"]) == 1
    assert out["review_items"][0].field_name == "total_amount"


def test_route_confidence_gate() -> None:
    from newfan_schemas import ReviewItem

    assert (
        nodes.route_confidence_gate({"review_items": [ReviewItem(field_name="x", reason="r")]})
        == "hitl_review"
    )
    assert nodes.route_confidence_gate({"review_items": []}) == "finalize"
