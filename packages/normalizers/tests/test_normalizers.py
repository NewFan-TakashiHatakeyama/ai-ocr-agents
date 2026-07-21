import pytest
from newfan_schemas import FieldType

from newfan_normalizers import NormContext, normalize


def test_string_nfkc_and_whitespace() -> None:
    r = normalize(FieldType.STRING, "  Ａ Ｂ　 C  ")
    assert r.value == "A B C"
    assert r.type_converted is False


def test_date_wareki_to_seireki() -> None:
    r = normalize(FieldType.DATE, "令和6年5月1日")
    assert r.value == "2024-05-01"
    assert r.type_converted is True


def test_date_gannen() -> None:
    # 令和元年 = 令和1 = 2019
    assert normalize(FieldType.DATE, "令和元年12月3日").value == "2019-12-03"


def test_date_heisei() -> None:
    # 平成31年 = 2019
    assert normalize(FieldType.DATE, "平成31年4月1日").value == "2019-04-01"


def test_date_seirekireformat() -> None:
    r = normalize(FieldType.DATE, "2024/5/1")
    assert r.value == "2024-05-01"
    assert r.type_converted is True


def test_date_already_iso_exact() -> None:
    r = normalize(FieldType.DATE, "2024-05-01")
    assert r.value == "2024-05-01"
    assert r.type_converted is False  # 表記が変わらない → exact 扱い


def test_date_year_completion_caps_confidence() -> None:
    r = normalize(FieldType.DATE, "5月1日", NormContext(context_year=2024))
    assert r.value == "2024-05-01"
    assert r.confidence_cap == 0.85


# --- 和暦の略記（元号1文字＋区切り記号） ---
# 区切りを 年/月/日 に固定していたため「令02/01/31」が和暦と認識されず素通りしていた。
# ゴールデンセットを実 AWS に流して初めて発覚（closing_date / payment_due）。
# 元号 1 文字（令/平/昭）とアルファベット略記（R/H/S）も同じ穴に落ちていた。


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # 元号1文字＋スラッシュ（実帳票 sample2.png の締切日/支払期限）
        ("令02/01/31", "2020-01-31"),
        ("令02/02/29", "2020-02-29"),  # 2020 は閏年
        ("平31/4/1", "2019-04-01"),
        ("昭63/1/7", "1988-01-07"),
        # 元号2文字＋スラッシュ
        ("令和02/01/31", "2020-01-31"),
        ("平成31/4/1", "2019-04-01"),
        ("昭和63/1/7", "1988-01-07"),
        # アルファベット略記
        ("R2/1/31", "2020-01-31"),
        ("H31/4/1", "2019-04-01"),
        ("S63/1/7", "1988-01-07"),
        # ハイフン区切り
        ("令2-1-31", "2020-01-31"),
        ("令和2-1-31", "2020-01-31"),
        ("H31-4-1", "2019-04-01"),
        # ドット区切り
        ("令2.1.31", "2020-01-31"),
        ("昭63.1.7", "1988-01-07"),
        # 年月日（従来から通っていた形。区切りを増やして壊していないこと）
        ("令和2年1月31日", "2020-01-31"),
        ("令2年1月31日", "2020-01-31"),
        ("平成31年4月1日", "2019-04-01"),
        ("R2年1月31日", "2020-01-31"),
        # 元年
        ("令元/5/1", "2019-05-01"),
        ("令和元年12月3日", "2019-12-03"),
        # 前後に文字があっても拾う（実 KIE は「締切日：令02/01/31」の形で返す）
        ("締切日：令02/01/31", "2020-01-31"),
        ("お支払期限：令02/02/29", "2020-02-29"),
        ("請求日付令和02年01月31日", "2020-01-31"),
    ],
)
def test_date_wareki_variants(raw: str, expected: str) -> None:
    r = normalize(FieldType.DATE, raw)
    assert r.value == expected
    assert r.type_converted is True


@pytest.mark.parametrize(
    "raw",
    [
        # 〒 を T と誤認した郵便番号。T=大正 と読んで 1911-… にされると、
        # OCR の誤読が「もっともらしい日付」に化けて発見が遅れる。
        "T222-0001",
        "TEL:044-999-9999",
        "FAX:044-999-9999",
        # 元号らしき文字の後ろが数字でない
        "令和のできごと",
        "大阪日日銀行 麹町支店",
    ],
)
def test_date_wareki_does_not_match_lookalikes(raw: str) -> None:
    r = normalize(FieldType.DATE, raw)
    assert r.value == raw
    assert r.type_converted is False


def test_money_basic() -> None:
    r = normalize(FieldType.MONEY_JPY, "¥128,000")
    assert r.value == "128000"
    assert r.type_converted is True


def test_money_fullwidth_and_yen_kanji() -> None:
    assert normalize(FieldType.MONEY_JPY, "１２８，０００円").value == "128000"


def test_money_negative_triangle() -> None:
    assert normalize(FieldType.MONEY_JPY, "△1,200").value == "-1200"
    assert normalize(FieldType.MONEY_JPY, "▲1,200").value == "-1200"


def test_money_decimal_flagged_not_converted() -> None:
    r = normalize(FieldType.MONEY_JPY, "128.000")
    assert r.needs_review_hint == "decimal_point_ambiguous"
    # 自動変換しない（"." を保持）
    assert "." in (r.value or "")


def test_number_with_unit() -> None:
    r = normalize(FieldType.NUMBER, "３個")
    assert r.value == "3"
    assert r.extra["unit"] == "個"


def test_tax_rate_reduced() -> None:
    r = normalize(FieldType.TAX_RATE_JP, "8%(軽)")
    assert r.extra == {"rate": 8, "reduced_flag": True}
    assert r.value == "8"


def test_tax_rate_standard() -> None:
    r = normalize(FieldType.TAX_RATE_JP, "10%")
    assert r.extra["rate"] == 10
    assert r.extra["reduced_flag"] is False


def test_reg_no_format() -> None:
    r = normalize(FieldType.JP_INVOICE_REG_NO, "1234567890123")
    assert r.value == "T1234567890123"
    assert r.needs_review_hint is None


def test_reg_no_confusable_flagged() -> None:
    # O が混入 → 自動変換せず LLM 補正候補
    r = normalize(FieldType.JP_INVOICE_REG_NO, "T12345678901O3")
    assert r.needs_review_hint == "confusable_chars"


def test_bank_account_decompose() -> None:
    r = normalize(FieldType.JP_BANK_ACCOUNT, "みずほ銀行 0001 支店 001 普通 1234567")
    assert r.extra["bank_code"] == "0001"
    assert r.extra["branch_code"] == "001"
    assert r.extra["account_type"] == "普通"
    assert r.extra["account_number"] == "1234567"


def test_none_input() -> None:
    assert normalize(FieldType.STRING, None).value is None
