"""組込み正規化器（詳細設計 §5.6）。

各正規化器は (value_raw, NormContext) -> NormalizationResult。純ロジック（決定論）。
"""

from __future__ import annotations

import re
import unicodedata

from newfan_normalizers.result import NormalizationResult, NormContext

# 和暦→西暦の基準（year = base + n）。令和1=2019, 平成1=1989（§5.6）。
# 帳票では元号 1 文字の略記（令02/01/31）も普通に使われるため別名として持つ。
_ERA_BASE = {
    "令和": 2018,
    "平成": 1988,
    "昭和": 1925,
    "大正": 1911,
    "明治": 1867,
    "令": 2018,
    "平": 1988,
    "昭": 1925,
    "大": 1911,
    "明": 1867,
    "R": 2018,
    "H": 1988,
    "S": 1925,
    "T": 1911,
    "M": 1867,
}

# 長いものから並べる。"令" を先に置くと "令和" が "令"+"和…" と解釈されて年が読めない。
_ERA_ALT = "|".join(sorted(_ERA_BASE, key=len, reverse=True))

# 区切りは 年/月/日 だけでなく / - . も受ける。実帳票の「令02/01/31」「R2/1/31」は
# 年月日を伴わないため、区切りを 年月 に固定していた頃は和暦と認識できず素通りしていた。
# (?<![0-9A-Za-z]) は "T222-0001"（〒の T 誤認）のような英数字の途中を元号と
# 読み違えないための番人。
_WAREKI = re.compile(
    rf"(?<![0-9A-Za-z])({_ERA_ALT})\s*(元|\d{{1,2}})\s*(?:年|[/.-])\s*(\d{{1,2}})"
    r"\s*(?:月|[/.-])\s*(\d{1,2})\s*日?"
)
_SEIREKI = re.compile(r"(\d{4})\s*[年/.\-]\s*(\d{1,2})\s*[月/.\-]\s*(\d{1,2})\s*日?")
_MD_ONLY = re.compile(r"^\s*(\d{1,2})\s*[月/.\-]\s*(\d{1,2})\s*日?\s*$")


def _nfkc(value: str) -> str:
    return unicodedata.normalize("NFKC", value)


def _fmt_date(
    year: int, month: str, day: str, *, type_converted: bool, cap: float | None = None
) -> NormalizationResult:
    iso = f"{year:04d}-{int(month):02d}-{int(day):02d}"
    return NormalizationResult(value=iso, type_converted=type_converted, confidence_cap=cap)


def norm_string(value: str, ctx: NormContext) -> NormalizationResult:
    v = _nfkc(value)
    v = re.sub(r"\s+", " ", v).strip()
    return NormalizationResult(value=v)


def norm_date(value: str, ctx: NormContext) -> NormalizationResult:
    s = _nfkc(value).strip()

    m = _WAREKI.search(s)
    if m:
        era, yr, mo, da = m.groups()
        n = 1 if yr == "元" else int(yr)
        return _fmt_date(_ERA_BASE[era] + n, mo, da, type_converted=True)

    m = _SEIREKI.search(s)
    if m:
        y, mo, da = m.groups()
        res = _fmt_date(int(y), mo, da, type_converted=False)
        # 表記が変わった場合のみ型変換扱い（例: 2024/5/1 → 2024-05-01）
        res.type_converted = res.value != s
        return res

    m = _MD_ONLY.match(s)
    if m and ctx.context_year is not None:
        mo, da = m.groups()
        # 年補完時は confidence 上限 0.85（§5.6）
        return _fmt_date(ctx.context_year, mo, da, type_converted=True, cap=0.85)

    return NormalizationResult(value=s)


def norm_money_jpy(value: str, ctx: NormContext) -> NormalizationResult:
    s = _nfkc(value)
    neg = s.strip().startswith("-") or "△" in s or "▲" in s
    body = (
        s.replace("¥", "")
        .replace("￥", "")
        .replace("円", "")
        .replace(",", "")
        .replace("△", "")
        .replace("▲", "")
        .replace(" ", "")
        .replace("-", "")
    )

    # 小数点は JPY では桁区切り誤認の可能性 → 自動変換せず LLM 補正候補（§5.6）
    if "." in body:
        val = ("-" if neg else "") + body
        return NormalizationResult(
            value=val, type_converted=True, needs_review_hint="decimal_point_ambiguous"
        )

    m = re.search(r"\d+", body)
    if not m:
        return NormalizationResult(value=None)
    val = ("-" if neg else "") + m.group()
    return NormalizationResult(value=val, type_converted=True)


def norm_number(value: str, ctx: NormContext) -> NormalizationResult:
    s = _nfkc(value).strip()
    stripped = s.replace(",", "")
    m = re.search(r"-?\d+(?:\.\d+)?", stripped)
    if not m:
        return NormalizationResult(value=None)
    num = m.group()
    unit = stripped[m.end() :].strip()
    extra = {"unit": unit} if unit else {}
    return NormalizationResult(value=num, type_converted=num != s, extra=extra)


def norm_tax_rate_jp(value: str, ctx: NormContext) -> NormalizationResult:
    s = _nfkc(value)
    reduced = ("軽" in s) or ("※" in s)
    m = re.search(r"(\d+(?:\.\d+)?)\s*%?", s)
    if not m:
        return NormalizationResult(value=None, extra={"reduced_flag": reduced})
    rate_f = float(m.group(1))
    rate: float | int = int(rate_f) if rate_f == int(rate_f) else rate_f
    return NormalizationResult(
        value=str(rate),
        type_converted=True,
        extra={"rate": rate, "reduced_flag": reduced},
    )


def norm_jp_invoice_reg_no(value: str, ctx: NormContext) -> NormalizationResult:
    s = re.sub(r"[\s\-]", "", _nfkc(value).upper())
    body = s[1:] if s.startswith("T") else s
    hint = None
    # 混同文字（O/I/L/B 等）は自動変換せず LLM 補正候補（DD-10）
    if re.search(r"[^0-9]", body):
        hint = "confusable_chars"
    return NormalizationResult(value="T" + body, type_converted=True, needs_review_hint=hint)


_ACCOUNT_TYPES = ("普通", "当座", "貯蓄")


def norm_jp_bank_account(value: str, ctx: NormContext) -> NormalizationResult:
    s = _nfkc(value)
    account_type = next((k for k in _ACCOUNT_TYPES if k in s), None)
    groups = re.findall(r"\d+", s)

    bank = next((g for g in groups if len(g) == 4), None)
    branch = next((g for g in groups if len(g) == 3), None)
    account = next((g for g in groups if len(g) == 7), None)

    extra = {
        "bank_code": bank,
        "branch_code": branch,
        "account_type": account_type,
        "account_number": account,
        "digit_groups": groups,
    }
    parts = [p for p in (bank, branch, account_type, account) if p]
    return NormalizationResult(
        value="/".join(parts) if parts else None, type_converted=True, extra=extra
    )
