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


# ---------------- 住所（ADR-0007） ----------------

# 郵便番号の 7 桁。ハイフンの周りの空白を許す（「〒100 0001」「〒 100 - 0001」── OCR が
# ハイフンを落とす／空白を挟む）。ここで許さないと、剥がし（規則 2）が空白の畳み込み（規則 3）
# より先に走る都合で「〒」だけ剥がれて数字が本文に残り、2 回目の適用で初めて剥がれる
# ＝不動点でなくなる。不動点であることは正解データの検査と計測の採点が前提にしている。
_ADDR_POSTAL_DIGITS = r"\d{3}\s*-?\s*\d{4}"
# 先頭の郵便番号。〒 は有っても無くてもよい。後ろに数字や "-" が続くものは郵便番号では
# ない（"1234567-8" のような番地の頭を食わない）。
_ADDR_POSTAL_HEAD = re.compile(rf"^(?:〒\s*)?{_ADDR_POSTAL_DIGITS}(?![\d-])\s*")
# 末尾の郵便番号。**本文と空白か 〒 で切れているものだけ**を落とす。"…町123-4567" のように
# 番地が 3+4 桁で終わる住所を郵便番号と誤認して削らないための番人。
_ADDR_POSTAL_TAIL = re.compile(rf"(?:\s+(?:〒\s*)?|〒\s*){_ADDR_POSTAL_DIGITS}$")
# 〒 だけ（OCR が数字を落とした／郵便番号が別項目で読まれた）。
_ADDR_POSTAL_MARK_HEAD = re.compile(r"^〒\s*")
_ADDR_POSTAL_MARK_TAIL = re.compile(r"\s*〒$")
# 先頭の見出し語。帳票の住所欄はラベルと値が同じ行に並ぶことが多く、span を連結した
# value_raw に「本社 〒…」「住所：…」の形で混ざる。**先頭だけ**を見る（「住所1住所2」の
# ようにダミー住所の途中に出る語には触らない）。
#
# 見出し語の**後ろ**も見る。「所在地東京都…」のように区切りが無くても剥がしたいが、区切りを
# 要求しないと「本社工場 愛知県…」「本社ビル 東京都…」「支店名: 東京都…」のような**長い
# 複合語の先頭 2〜3 文字だけ**を剥がして「工場愛知県…」「名:東京都…」を作ってしまう
# （見出し語付きのままより悪い）。剥がすのは、見出し語の後ろが「区切り／空白／〒／数字
# （郵便番号）／末尾」か、住所の本文の頭（都道府県名か、短い市区町村郡名）のときだけ。
# 市区町村郡名を 3 文字までに絞るのは、「工場愛知県豊田市」のような複合語＋住所を
# 「…市」で終わる名前と見なさないため。
_ADDR_BODY_HEAD = (
    r"[:：\s〒\d]|$|北海道|東京都|京都府|大阪府|[一-鿿]{2,3}県|[一-鿿]{1,3}[市区町村郡]"
)
_ADDR_LABEL_HEAD = re.compile(
    rf"^(?:住所|所在地|本社|本店|支社|支店|営業所|事業所|(?i:address))(?={_ADDR_BODY_HEAD})[:：]?\s*"
)
# 番地の区切りに使われるハイフン類。**数字と数字の間にあるものだけ** "-" に揃える。
# 長音「ー」は「タワー」「ビルディング」の一部でもあるので、無条件に置換してはならない。
# 全角ハイフン "－"（U+FF0D）は NFKC が "-" にするのでここには含めない。
_ADDR_DASH_BETWEEN_DIGITS = re.compile(r"(?<=\d)[‐‑‒–—―−ー](?=\d)")
# 空白。**両隣が半角英数字のものだけ** 1 つの半角空白に畳み、それ以外は除く。無条件に
# 除くと「1-1-1 3F」（番地の直後の階数）が「1-1-13F」（13 階／13 番地）に、英字の住所
# 「A-123 Sample.BLD」が「A-123Sample.BLD」に化けて、別の住所になる。日本語の住所では
# 空白は語の境界を担わない（「東京都 品川区」「9-9-9 ファッションビル」）ので除いてよい。
_ADDR_WS = re.compile(r"\s+")
_ADDR_ALNUM = re.compile(r"[0-9A-Za-z]")


def _squash_address_ws(s: str) -> str:
    def repl(m: re.Match[str]) -> str:
        i, j = m.start(), m.end()
        if i > 0 and j < len(s) and _ADDR_ALNUM.match(s[i - 1]) and _ADDR_ALNUM.match(s[j]):
            return " "
        return ""

    return _ADDR_WS.sub(repl, s)


def norm_address_jp(value: str, ctx: NormContext) -> NormalizationResult:
    """日本の住所を「郵便番号なし・見出し語なし・都道府県〜建物名/階まで」の形に揃える。

    第 3 回計測（docs/design/region-measurement-2026-09-12.md）で住所の不正解の多くが
    「郵便番号や『本社』を値に含めるか」という**値の慣例**の食い違いだった。慣例を
    正規化器で 1 つに決め、正解データも同じ形で持つ（ADR-0007）。

    規則（順に適用）:
      1. NFKC（全角英数字・全角ハイフン・全角空白を揃える）
      2. 先頭の見出し語（住所／所在地／本社／本店／支社／支店／営業所／事業所／Address。
         後ろに住所の本文か区切りが続くものだけ ── 「本社工場」「支店名」は剥がさない）と
         先頭・末尾の郵便番号（〒 は任意。〒 だけのものも）を、剥がれなくなるまで剥がす
         ── 「本社 981-3205 仙台市…」は見出し語の後ろに郵便番号が続くので 1 回では足りない
      3. 空白（全角含む）は、両隣が半角英数字のものだけ 1 つの半角空白に畳み、それ以外は除く
         ── 「1-1-1 3F」の階数や「A-123 Sample.BLD」の語の境界を残す
      4. 数字と数字の間のハイフン類（‐ ‑ ‒ – — ― − ー）を "-" に揃える
      5. 何も残らなければ None

    結果は**不動点**（もう一度通しても変わらない）。正解データの検査
    （golden/scripts/check_gold_addresses.py）と計測の採点（region_ab.score_key は
    プロダクトの value_normalized にもう一度これを通す）がそれを前提にしている。

    **建物名・階は削らない**（正解の慣例）。「東京都品川区北品川5-10-20」のように建物名が
    落ちた抽出値は、慣例の問題ではなく本当の取りこぼしなので、正規化で隠さない。
    値の形を変えるだけで導出はしないので type_converted は False（grounding の 0.85 に
    落とさない）。confidence_cap も付けない。
    """
    s = _nfkc(value).strip()
    while True:
        before = s
        s = _ADDR_LABEL_HEAD.sub("", s, count=1)
        s = _ADDR_POSTAL_HEAD.sub("", s, count=1)
        s = _ADDR_POSTAL_MARK_HEAD.sub("", s, count=1)
        s = _ADDR_POSTAL_TAIL.sub("", s, count=1)
        s = _ADDR_POSTAL_MARK_TAIL.sub("", s, count=1)
        s = s.strip()
        if s == before:
            break
    s = _squash_address_ws(s)
    s = _ADDR_DASH_BETWEEN_DIGITS.sub("-", s)
    return NormalizationResult(value=s or None, type_converted=False)
