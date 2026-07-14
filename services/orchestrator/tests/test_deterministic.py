"""deterministic_normalize → confidence_score → validate の配線テスト（§5.6/§5.7）。"""

from newfan_schemas import ExtractedField, Span, TableCell, TableResult

from newfan_orchestrator import nodes

_SCHEMA = {
    "doc_type": "invoice",
    "fields": [
        {"name": "invoice_date", "type": "date", "critical": True},
        {"name": "total_amount", "type": "money_jpy", "critical": True},
        {"name": "registration_no", "type": "jp_invoice_reg_no", "critical": True},
        {"name": "subtotal", "type": "money_jpy"},
        {"name": "tax_amount", "type": "money_jpy"},
    ],
}


def test_deterministic_normalize_sets_value_and_meta() -> None:
    fields = [
        ExtractedField(name="invoice_date", value_raw="令和6年5月1日", span_ids=[0]),
        ExtractedField(name="total_amount", value_raw="¥128,000", span_ids=[1]),
    ]
    out = nodes.deterministic_normalize({"schema": _SCHEMA, "fields": fields})
    by_name = {f.name: f for f in out["fields"]}
    assert by_name["invoice_date"].value_normalized == "2024-05-01"
    assert by_name["total_amount"].value_normalized == "128000"
    # 和暦→西暦は type_converted
    assert out["norm_meta"]["invoice_date"]["type_converted"] is True


def test_confidence_uses_type_converted_for_grounding() -> None:
    span = Span(span_id=1, page=1, text="¥128,000", conf=0.95, bbox=[0, 0, 1, 1])
    field = ExtractedField(
        name="total_amount", value_normalized="128000", source_quote="¥128,000", span_ids=[1]
    )
    state = {
        "spans": [span],
        "fields": [field],
        "norm_meta": {"total_amount": {"type_converted": True, "confidence_cap": None}},
    }
    out = nodes.confidence_score(state)
    # exact 一致しないが型変換で導出 → grounding 0.85
    assert out["fields"][0].grounding_score == 0.85
    assert out["fields"][0].confidence == 0.85  # min(0.95, 0.85)


def test_confidence_cap_applied() -> None:
    span = Span(span_id=1, page=1, text="5月1日", conf=0.99, bbox=[0, 0, 1, 1])
    field = ExtractedField(
        name="invoice_date", value_normalized="2024-05-01", source_quote="5月1日", span_ids=[1]
    )
    state = {
        "spans": [span],
        "fields": [field],
        "norm_meta": {"invoice_date": {"type_converted": True, "confidence_cap": 0.85}},
    }
    out = nodes.confidence_score(state)
    assert out["fields"][0].confidence <= 0.85


def test_validate_elevates_on_sum_pass() -> None:
    fields = [
        ExtractedField(name="subtotal", value_normalized="300", confidence=0.5),
        ExtractedField(name="tax_amount", value_normalized="30", confidence=0.5),
        ExtractedField(name="total_amount", value_normalized="330", confidence=0.5),
    ]
    table = TableResult(
        name="line_items",
        rows=[
            {
                "qty": TableCell(value="2"),
                "unit_price": TableCell(value="100"),
                "amount": TableCell(value="200"),
                "tax_rate": TableCell(value="10"),
            },
            {
                "qty": TableCell(value="1"),
                "unit_price": TableCell(value="100"),
                "amount": TableCell(value="100"),
                "tax_rate": TableCell(value="10"),
            },
        ],
    )
    out = nodes.validate({"fields": fields, "tables": [table]})
    by_name = {f.name: f for f in out["fields"]}
    # V-SUM 合格 → 金額フィールドが auto-elevation（0.98 へ）
    assert by_name["total_amount"].confidence == 0.98
    assert by_name["total_amount"].validation is not None
    assert by_name["total_amount"].validation["passed"] is True


def test_validate_regno_failure_recorded() -> None:
    fields = [ExtractedField(name="registration_no", value_normalized="T1123456789012")]
    out = nodes.validate({"fields": fields, "tables": []})
    v = out["fields"][0].validation
    assert v is not None and v["passed"] is False
