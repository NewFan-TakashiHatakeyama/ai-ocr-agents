"""決定論バリデータ・カタログ（詳細設計 §5.7.3）。

各チェックは純関数で list[CheckResult] を返す。丸めトレランスは ±1円/明細（§5.7.3）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Optional

from newfan_validators.result import CheckResult, Severity

_REGNO = re.compile(r"^T\d{13}$")


@dataclass
class LineItem:
    qty: Optional[float] = None
    unit_price: Optional[int] = None
    amount: Optional[int] = None
    tax_rate: Optional[float] = None


def parse_yen(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    text = str(value)
    neg = text.strip().startswith("-") or "△" in text or "▲" in text
    digits = re.sub(r"[^\d]", "", text)
    if not digits:
        return None
    n = int(digits)
    return -n if neg else n


def parse_num(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", ""))
    return float(m.group()) if m else None


def corporate_number_check_digit(base12: str) -> int:
    """法人番号の検査用数字（国税庁仕様）。

    検査用数字 = 9 − ((Σ Pn×Qn) mod 9)。Pn は基礎番号(下位12桁)の最下位を1桁目とした n 桁目、
    Qn は n が奇数なら1・偶数なら2。
    """
    total = 0
    for i, ch in enumerate(reversed(base12)):
        n = i + 1
        q = 1 if n % 2 == 1 else 2
        total += int(ch) * q
    return 9 - (total % 9)


def v_regno(value: Optional[str], *, field_name: str = "registration_no") -> list[CheckResult]:
    if not value or not _REGNO.match(value):
        return [
            CheckResult(
                "V-REGNO", False, Severity.ERROR, "登録番号は T+13桁である必要があります", [field_name]
            )
        ]
    corp = value[1:]  # 13桁
    check = int(corp[0])
    base = corp[1:]  # 下位12桁
    if corporate_number_check_digit(base) != check:
        return [
            CheckResult(
                "V-REGNO", False, Severity.ERROR, "法人番号のチェックディジット不一致", [field_name]
            )
        ]
    return [CheckResult("V-REGNO", True, Severity.INFO, "OK", [field_name])]


def _parse_iso(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def v_date(
    invoice_date: Optional[str],
    due_date: Optional[str],
    today: date,
    *,
    inv_name: str = "invoice_date",
    due_name: str = "due_date",
) -> list[CheckResult]:
    results: list[CheckResult] = []
    inv = _parse_iso(invoice_date)
    due = _parse_iso(due_date)

    if invoice_date is not None and inv is None:
        results.append(CheckResult("V-DATE", False, Severity.ERROR, f"{inv_name} が不正な日付", [inv_name]))
    if due_date is not None and due is None:
        results.append(CheckResult("V-DATE", False, Severity.ERROR, f"{due_name} が不正な日付", [due_name]))
    if inv and due and inv > due:
        results.append(
            CheckResult("V-DATE", False, Severity.ERROR, "請求日が支払期日より後", [inv_name, due_name])
        )
    for name, d in ((inv_name, inv), (due_name, due)):
        if d is None:
            continue
        if d > today:
            results.append(CheckResult("V-DATE", True, Severity.WARNING, f"{name} が未来日", [name]))
        elif (today - d).days > 3650:
            results.append(
                CheckResult("V-DATE", True, Severity.WARNING, f"{name} が10年超過去", [name])
            )

    # 問題なし・日付ありなら合格を記録する（§6.3 の validation.checks に反映するため）
    if not results and (inv is not None or due is not None):
        present = [n for n, d in ((inv_name, inv), (due_name, due)) if d is not None]
        results.append(CheckResult("V-DATE", True, Severity.INFO, "OK", present))
    return results


def v_bank(value: Optional[str], *, field_name: str = "bank_account") -> list[CheckResult]:
    if not value:
        return [CheckResult("V-BANK", False, Severity.ERROR, "口座情報なし", [field_name])]
    groups = re.findall(r"\d+", value)
    bank = next((g for g in groups if len(g) == 4), None)
    branch = next((g for g in groups if len(g) == 3), None)
    account = next((g for g in groups if len(g) == 7), None)
    acct_type = next((k for k in ("普通", "当座", "貯蓄") if k in value), None)
    ok = bool(bank and branch and account and acct_type)
    msg = "OK" if ok else "銀行4桁/支店3桁/口座7桁/種別のいずれか不足"
    return [CheckResult("V-BANK", ok, Severity.INFO if ok else Severity.ERROR, msg, [field_name])]


def v_qty(items: list[LineItem], *, field_name: str = "line_items") -> list[CheckResult]:
    bad: list[int] = []
    computed = 0
    for i, li in enumerate(items):
        if li.qty is None or li.unit_price is None or li.amount is None:
            continue
        computed += 1
        if abs(li.qty * li.unit_price - li.amount) > 1:
            bad.append(i + 1)
    ok = not bad
    msg = "数量×単価=金額 OK" if ok else f"行 {bad} で数量×単価≠金額"
    return [
        CheckResult(
            "V-QTY",
            ok,
            Severity.INFO if ok else Severity.ERROR,
            msg,
            [field_name],
            elevates=ok and computed > 0,
        )
    ]


def v_sum(
    item_amounts: list[int],
    subtotal: Optional[int],
    tax: Optional[int],
    total: Optional[int],
    *,
    money_fields: Optional[list[str]] = None,
) -> list[CheckResult]:
    fields = money_fields or ["total_amount", "subtotal", "tax_amount"]
    results: list[CheckResult] = []
    passed_all = True

    if item_amounts and subtotal is not None:
        tol = max(1, len(item_amounts))  # ±1円/明細
        ok = abs(sum(item_amounts) - subtotal) <= tol
        passed_all = passed_all and ok
        results.append(
            CheckResult("V-SUM", ok, Severity.INFO if ok else Severity.ERROR, "明細合計=小計", fields)
        )
    if subtotal is not None and tax is not None and total is not None:
        ok = abs(subtotal + tax - total) <= 1
        passed_all = passed_all and ok
        results.append(
            CheckResult("V-SUM", ok, Severity.INFO if ok else Severity.ERROR, "小計+税=合計", fields)
        )

    # 全 V-SUM 合格時、関連金額フィールドを auto-elevation 対象に（§5.7.3）
    if results and passed_all:
        for r in results:
            r.elevates = True
    return results


def v_tax(
    by_rate: dict[float, tuple[int, int]], *, field_name: str = "tax_amount"
) -> list[CheckResult]:
    """税率別 {rate%: (課税対象額, 税額)} を検算する。"""
    results: list[CheckResult] = []
    for rate, (taxable, tax) in by_rate.items():
        expected = taxable * rate / 100.0
        ok = abs(expected - tax) <= 1
        results.append(
            CheckResult(
                "V-TAX",
                ok,
                Severity.INFO if ok else Severity.ERROR,
                f"税率{rate}% 課税対象額×税率≒税額",
                [field_name],
            )
        )
    return results


def v_dup(
    key: tuple[Optional[str], ...], exists: bool, *, fields: Optional[list[str]] = None
) -> list[CheckResult]:
    """（発行者×請求番号×金額）の重複検知。exists は外部ルックアップ結果。"""
    fields = fields or ["issuer_name", "invoice_no", "total_amount"]
    if exists:
        return [CheckResult("V-DUP", True, Severity.WARNING, f"重複の可能性: {key}", fields)]
    return [CheckResult("V-DUP", True, Severity.INFO, "重複なし", fields)]
