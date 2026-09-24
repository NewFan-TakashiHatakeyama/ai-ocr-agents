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


def test_deterministic_normalize_address_jp_strips_postal_code() -> None:
    """address_jp（ADR-0007）はレジストリから引かれ、郵便番号・見出し語を落とす。

    値の形を変えるだけで導出はしないので type_converted は False（grounding を
    0.85 に落とさない）。"""
    schema = {
        "doc_type": "invoice",
        "fields": [{"name": "customer_address", "type": "address_jp"}],
    }
    fields = [
        ExtractedField(
            name="customer_address", value_raw="本社 〒981-3205 仙台市泉区紫山3-1-4", span_ids=[0]
        )
    ]
    out = nodes.deterministic_normalize({"schema": schema, "fields": fields})
    assert out["fields"][0].value_normalized == "仙台市泉区紫山3-1-4"
    assert out["norm_meta"]["customer_address"]["type_converted"] is False
    assert out["norm_meta"]["customer_address"]["confidence_cap"] is None


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


def _two_line_address_state(schema: dict, value_raw: str) -> dict:
    """sample2.png の宛先住所（2 行・2 span）を kie の出力形で組む。

    source_quote は kie と同じく span テキストの半角空白連結。
    """
    spans = [
        Span(span_id=3, page=1, text="神奈川県横浜市港北区樽町", conf=0.9713, bbox=[0, 0, 1, 1]),
        Span(span_id=4, page=1, text="エイピービル", conf=0.9330, bbox=[0, 1, 1, 2]),
    ]
    field = ExtractedField(
        name="recipient_address",
        value_raw=value_raw,
        span_ids=[3, 4],
        source_quote=" ".join(s.text for s in spans),
    )
    return {"schema": schema, "spans": spans, "fields": [field]}


def test_confidence_multiline_value_schemaless() -> None:
    """スキーマなし抽出（ADR-0006）で LLM が 2 行をつないで返しても grounding 1.0。

    実データ（run_dda2fb931f3a4e8c8ed6dfc5）では grounding 0・confidence 0.00 に落ち、
    「根拠 span なし」の強制レビューに回っていた。
    """
    state = _two_line_address_state(
        {"doc_type": "", "fields": []}, "神奈川県横浜市港北区樽町エイピービル"
    )
    state.update(nodes.deterministic_normalize(state))
    out = nodes.confidence_score(state)
    f = out["fields"][0]
    assert f.grounding_score == 1.0
    assert f.confidence > 0.9


def test_confidence_multiline_value_address_jp() -> None:
    """address_jp（ADR-0007）は日本語に接する空白を除くので、根拠の span 区切りと食い違う。

    同じ線引きで畳んで比べるので、改行入りで返っても grounding 1.0。
    """
    schema = {
        "doc_type": "invoice",
        "fields": [{"name": "recipient_address", "type": "address_jp"}],
    }
    state = _two_line_address_state(schema, "神奈川県横浜市港北区樽町\nエイピービル")
    state.update(nodes.deterministic_normalize(state))
    assert state["fields"][0].value_normalized == "神奈川県横浜市港北区樽町エイピービル"
    out = nodes.confidence_score(state)
    assert out["fields"][0].grounding_score == 1.0


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
