"""バリデーション実行の glue（§4.3 validate ノード用）。

標準的な帳票フィールド名（§5.5 例）を前提に ExtractedField / TableResult から
各 V-* チェックの入力を取り出して呼ぶ。入力が揃わないチェックはスキップする。
"""

from __future__ import annotations

from datetime import date
from typing import Callable, Optional

from newfan_schemas import ExtractedField, TableResult

from newfan_validators.checks import (
    LineItem,
    parse_num,
    parse_yen,
    v_bank,
    v_date,
    v_dup,
    v_qty,
    v_regno,
    v_sum,
    v_tax,
)
from newfan_validators.result import CheckResult

# 標準フィールド名（テナントスキーマで異なる場合は呼び出し側で names を差し替える）
DEFAULT_NAMES = {
    "registration_no": "registration_no",
    "invoice_date": "invoice_date",
    "due_date": "due_date",
    "bank_account": "bank_account",
    "subtotal": "subtotal",
    "tax_amount": "tax_amount",
    "total_amount": "total_amount",
    "issuer_name": "issuer_name",
    "invoice_no": "invoice_no",
    "line_items": "line_items",
}

DupLookup = Callable[[tuple[Optional[str], ...]], bool]


def _extract_line_items(tables: list[TableResult], table_name: str) -> list[LineItem]:
    items: list[LineItem] = []
    for table in tables:
        if table.name != table_name:
            continue
        for row in table.rows:
            def cell(key: str) -> Optional[str]:
                c = row.get(key)
                return c.value if c is not None else None

            items.append(
                LineItem(
                    qty=parse_num(cell("qty")),
                    unit_price=parse_yen(cell("unit_price")),
                    amount=parse_yen(cell("amount")),
                    tax_rate=parse_num(cell("tax_rate")),
                )
            )
    return items


def _group_taxable_by_rate(items: list[LineItem]) -> dict[float, int]:
    by_rate: dict[float, int] = {}
    for li in items:
        if li.tax_rate is None or li.amount is None:
            continue
        by_rate[li.tax_rate] = by_rate.get(li.tax_rate, 0) + li.amount
    return by_rate


def run_validations(
    fields: list[ExtractedField],
    tables: list[TableResult],
    *,
    today: Optional[date] = None,
    dup_lookup: Optional[DupLookup] = None,
    names: Optional[dict[str, str]] = None,
) -> list[CheckResult]:
    today = today or date.today()
    names = names or DEFAULT_NAMES

    def val(key: str) -> Optional[str]:
        name = names.get(key, key)
        for f in fields:
            if f.name == name:
                return f.value_normalized if f.value_normalized is not None else f.value_raw
        return None

    present = {f.name for f in fields}
    results: list[CheckResult] = []

    if names["registration_no"] in present:
        results += v_regno(val("registration_no"), field_name=names["registration_no"])

    if names["invoice_date"] in present or names["due_date"] in present:
        results += v_date(
            val("invoice_date"),
            val("due_date"),
            today,
            inv_name=names["invoice_date"],
            due_name=names["due_date"],
        )

    if names["bank_account"] in present:
        results += v_bank(val("bank_account"), field_name=names["bank_account"])

    line_items = _extract_line_items(tables, names["line_items"])
    if line_items:
        results += v_qty(line_items, field_name=names["line_items"])
        amounts = [li.amount for li in line_items if li.amount is not None]
        results += v_sum(
            amounts,
            parse_yen(val("subtotal")),
            parse_yen(val("tax_amount")),
            parse_yen(val("total_amount")),
            money_fields=[names["total_amount"], names["subtotal"], names["tax_amount"]],
        )
        by_rate = _group_taxable_by_rate(line_items)
        tax_value = parse_yen(val("tax_amount"))
        if len(by_rate) == 1 and tax_value is not None:
            rate, taxable = next(iter(by_rate.items()))
            results += v_tax({rate: (taxable, tax_value)}, field_name=names["tax_amount"])

    if dup_lookup is not None:
        key = (val("issuer_name"), val("invoice_no"), val("total_amount"))
        results += v_dup(
            key,
            dup_lookup(key),
            fields=[names["issuer_name"], names["invoice_no"], names["total_amount"]],
        )

    return results
