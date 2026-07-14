"""正規化器レジストリ（§5.5: type は正規化器のレジストリキー）。"""

from __future__ import annotations

from typing import Callable, Optional

from newfan_schemas import FieldType

from newfan_normalizers import builtin
from newfan_normalizers.result import NormalizationResult, NormContext

Normalizer = Callable[[str, NormContext], NormalizationResult]

REGISTRY: dict[FieldType, Normalizer] = {
    FieldType.STRING: builtin.norm_string,
    FieldType.DATE: builtin.norm_date,
    FieldType.MONEY_JPY: builtin.norm_money_jpy,
    FieldType.NUMBER: builtin.norm_number,
    FieldType.TAX_RATE_JP: builtin.norm_tax_rate_jp,
    FieldType.JP_INVOICE_REG_NO: builtin.norm_jp_invoice_reg_no,
    FieldType.JP_BANK_ACCOUNT: builtin.norm_jp_bank_account,
}


def normalize(
    field_type: FieldType,
    value_raw: Optional[str],
    ctx: NormContext | None = None,
) -> NormalizationResult:
    """type に対応する正規化器を適用する。未登録 type は string 扱い。"""
    if value_raw is None:
        return NormalizationResult(value=None)
    fn = REGISTRY.get(field_type, builtin.norm_string)
    return fn(value_raw, ctx or NormContext())
