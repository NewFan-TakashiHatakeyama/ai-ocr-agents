from datetime import date

from newfan_schemas import ExtractedField, TableCell, TableResult

from newfan_validators import run_validations


def _invoice_fields() -> list[ExtractedField]:
    return [
        ExtractedField(name="registration_no", value_normalized="T7123456789012"),
        ExtractedField(name="invoice_date", value_normalized="2026-05-01"),
        ExtractedField(name="due_date", value_normalized="2026-05-31"),
        ExtractedField(name="subtotal", value_normalized="300"),
        ExtractedField(name="tax_amount", value_normalized="30"),
        ExtractedField(name="total_amount", value_normalized="330"),
    ]


def _line_items_table() -> TableResult:
    return TableResult(
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


def test_run_all_pass() -> None:
    results = run_validations(
        _invoice_fields(), [_line_items_table()], today=date(2026, 7, 14)
    )
    ids = {r.check_id for r in results}
    assert {"V-REGNO", "V-DATE", "V-QTY", "V-SUM", "V-TAX"} <= ids
    assert all(r.passed for r in results)
    # V-SUM/V-QTY は合格で elevates
    assert any(r.elevates for r in results if r.check_id in ("V-SUM", "V-QTY"))


def test_run_detects_sum_mismatch() -> None:
    fields = _invoice_fields()
    for f in fields:
        if f.name == "total_amount":
            f.value_normalized = "999"
    results = run_validations(fields, [_line_items_table()], today=date(2026, 7, 14))
    sum_fail = [r for r in results if r.check_id == "V-SUM" and not r.passed]
    assert sum_fail


def test_run_dup_lookup() -> None:
    fields = _invoice_fields() + [
        ExtractedField(name="issuer_name", value_normalized="株式会社サンプル"),
        ExtractedField(name="invoice_no", value_normalized="INV-1"),
    ]
    results = run_validations(
        fields, [], today=date(2026, 7, 14), dup_lookup=lambda key: True
    )
    dup = [r for r in results if r.check_id == "V-DUP"]
    assert dup and dup[0].severity.value == "warning"


def test_run_skips_absent_checks() -> None:
    # 登録番号・日付・明細なし → V-REGNO/V-DATE/V-QTY は出ない
    results = run_validations(
        [ExtractedField(name="note", value_normalized="備考")], [], today=date(2026, 7, 14)
    )
    assert results == []
