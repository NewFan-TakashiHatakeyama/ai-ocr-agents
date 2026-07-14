from datetime import date

from newfan_validators import (
    LineItem,
    corporate_number_check_digit,
    v_bank,
    v_date,
    v_dup,
    v_qty,
    v_regno,
    v_sum,
    v_tax,
)


def test_corporate_check_digit_known_example() -> None:
    # 国税庁の例: 基礎番号 123456789012 → 検査用数字 7
    assert corporate_number_check_digit("123456789012") == 7


def test_v_regno_valid() -> None:
    res = v_regno("T7123456789012")
    assert res[0].passed is True


def test_v_regno_bad_format() -> None:
    assert v_regno("1234567890123")[0].passed is False
    assert v_regno("T123")[0].passed is False


def test_v_regno_bad_checkdigit() -> None:
    # 検査用数字を 7→1 に改変 → 不一致
    assert v_regno("T1123456789012")[0].passed is False


def test_v_date_order() -> None:
    today = date(2026, 7, 14)
    res = v_date("2024-06-01", "2024-05-01", today)  # 請求日 > 支払期日
    assert any(not r.passed for r in res)


def test_v_date_valid_and_future_warning() -> None:
    today = date(2026, 7, 14)
    res = v_date("2027-01-01", None, today)
    assert res and res[0].severity.value == "warning"
    assert res[0].passed is True


def test_v_date_invalid() -> None:
    res = v_date("2024-13-40", None, date(2026, 7, 14))
    assert any(not r.passed for r in res)


def test_v_bank_ok() -> None:
    assert v_bank("0001/001/普通/1234567")[0].passed is True


def test_v_bank_missing_type() -> None:
    assert v_bank("0001/001/1234567")[0].passed is False


def test_v_qty_ok_elevates() -> None:
    items = [LineItem(qty=2, unit_price=100, amount=200)]
    res = v_qty(items)
    assert res[0].passed is True
    assert res[0].elevates is True


def test_v_qty_mismatch() -> None:
    items = [LineItem(qty=2, unit_price=100, amount=250)]
    res = v_qty(items)
    assert res[0].passed is False
    assert res[0].elevates is False


def test_v_qty_no_computable_does_not_elevate() -> None:
    res = v_qty([LineItem(qty=None, unit_price=None, amount=None)])
    assert res[0].elevates is False


def test_v_sum_pass_elevates() -> None:
    res = v_sum([100, 200], subtotal=300, tax=30, total=330)
    assert all(r.passed for r in res)
    assert all(r.elevates for r in res)


def test_v_sum_mismatch() -> None:
    res = v_sum([100, 200], subtotal=300, tax=30, total=999)
    assert any(not r.passed for r in res)
    # 一部不合格なら elevates は付かない
    assert all(not r.elevates for r in res)


def test_v_sum_item_tolerance() -> None:
    # ±1円/明細（2明細で ±2円）を許容
    res = v_sum([100, 200], subtotal=302, tax=0, total=302)
    sum_check = [r for r in res if "明細合計" in r.message][0]
    assert sum_check.passed is True


def test_v_tax_ok() -> None:
    res = v_tax({10.0: (1000, 100)})
    assert res[0].passed is True


def test_v_tax_mismatch() -> None:
    res = v_tax({10.0: (1000, 120)})
    assert res[0].passed is False


def test_v_dup_flag() -> None:
    assert v_dup(("A", "1", "100"), True)[0].severity.value == "warning"
    assert v_dup(("A", "1", "100"), False)[0].severity.value == "info"
