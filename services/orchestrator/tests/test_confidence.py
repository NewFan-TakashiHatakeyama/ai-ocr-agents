from typing import Any

from newfan_schemas import Span, SpanSource

from newfan_orchestrator.confidence import (
    apply_correction_confidence,
    auto_elevate,
    compute_confidence,
    evidence_ocr_confidence,
    evidence_source,
    grounding_score,
    ocr_confidence,
)


def test_grounding_exact_match() -> None:
    assert grounding_score("128000", "128000") == 1.00
    # NFKC 正規化して一致（全角）
    assert grounding_score("128000", "１２８０００") == 1.00


def test_grounding_type_conversion() -> None:
    assert grounding_score("2024-05-01", "令和6年5月1日", type_converted=True) == 0.85


def test_grounding_vl_capped() -> None:
    # DD-09: VL 由来は上限 0.7
    assert grounding_score("128000", "128000", source=SpanSource.VL) == 0.70


def test_grounding_partial() -> None:
    assert grounding_score("128000", "合計 128000 円") == 0.70


def test_grounding_none_without_quote() -> None:
    assert grounding_score("128000", None) == 0.00


# --- 複数行にまたがる値（空白類の違いの吸収） ---------------------------------
# 実データ（dev DB, sample2.png）: 根拠 span は行ごとに分かれ、kie が半角空白で連結する。
# スキーマなし run_dda2fb931f3a4e8c8ed6dfc5 では LLM が行をつないで返し grounding 0 に
# 落ちていた。スキーマあり run_ca1f3c11ca2b465a9c0e3877 では改行入りで返り 1.0 だった。
_RECIPIENT_QUOTE = "神奈川県横浜市港北区樽町 エイピービル"  # span 3 + span 4
_ISSUER_QUOTE = "東京都新宿区四谷9-9-9 サプライビル2F"  # span 21 + span 22
_RECIPIENT_JOINED = "神奈川県横浜市港北区樽町エイピービル"  # スキーマなし run の value
_PHONE_QUOTE = "TEL:03-9999-9999 FAX:03-9999-9999"  # span 23


def test_grounding_multiline_value_joined_without_separator() -> None:
    """改行位置の片側が日本語の文字・記号のとき（日本語の境界）、LLM が行を区切りなしで
    つないだ値も根拠と完全一致として扱う。

    改行位置の両側が半角英数字のときは一致させない
    （test_grounding_multiline_joined_at_alnum_boundary_stays_zero）。
    """
    assert grounding_score(_RECIPIENT_JOINED, _RECIPIENT_QUOTE) == 1.00  # 「町」|「エ」
    # 「9」|「サ」: 片側が日本語なら数字に接していても吸収する
    assert grounding_score("東京都新宿区四谷9-9-9サプライビル2F", _ISSUER_QUOTE) == 1.00


def test_grounding_multiline_joined_at_alnum_boundary_stays_zero() -> None:
    """改行位置の両側が半角英数字のとき、行を区切りなしでつないだ値は grounding 0 のまま。

    「12 500」（数量と単価）に「12500」を一致させないための意図的な線引きで、2 行の住所でも
    同じ規則が掛かる（docs/design/grounding-whitespace.md「残る課題」）。空白・改行で区切って
    返した値は一致する。
    """
    # 「…丸の内1-1-1」＋「JPタワー」（「1」|「J」）
    marunouchi = "東京都千代田区丸の内1-1-1 JPタワー"
    assert grounding_score("東京都千代田区丸の内1-1-1JPタワー", marunouchi) == 0.00
    assert grounding_score("東京都千代田区丸の内1-1-1\nJPタワー", marunouchi) == 1.00
    # 「…梅田3-1-3」＋「2F」（「3」|「2」）。つなぐと 3-1-32 番地と区別できない
    umeda = "大阪府大阪市北区梅田3-1-3 2F"
    assert grounding_score("大阪府大阪市北区梅田3-1-32F", umeda) == 0.00
    assert grounding_score("大阪府大阪市北区梅田3-1-3 2F", umeda) == 1.00
    # 英字の住所「Suite 200」＋「New York」（「0」|「N」）
    assert grounding_score("Suite 200New York", "Suite 200 New York") == 0.00


def test_grounding_multiline_value_with_newline() -> None:
    """改行（CRLF 含む）で区切った値も一致（スキーマあり run の値の形）。"""
    assert grounding_score("神奈川県横浜市港北区樽町\nエイピービル", _RECIPIENT_QUOTE) == 1.00
    assert grounding_score("東京都新宿区四谷9-9-9\r\nサプライビル2F", _ISSUER_QUOTE) == 1.00
    # 銀行口座の 3 行（span 10〜12）。行末の数字と次行の頭の漢字の間の改行も吸収する
    bank_quote = (
        "大東京銀行 四谷支店 普通 1234567 大阪日日銀行 麹町支店 普通 1234567 "
        "ルナ銀行 ス夕一支店 普通 1234567"
    )
    bank_value = (
        "大東京銀行 四谷支店 普通 1234567\n大阪日日銀行 麹町支店 普通 1234567\n"
        "ルナ銀行 ス夕一支店 普通 1234567"
    )
    assert grounding_score(bank_value, bank_quote) == 1.00


def test_grounding_fullwidth_space_and_runs() -> None:
    """全角空白・タブ・空白の連なり・前後の空白の違いでは落とさない。"""
    assert grounding_score("神奈川県横浜市港北区樽町　エイピービル", _RECIPIENT_QUOTE) == 1.00
    assert grounding_score(_RECIPIENT_JOINED, "神奈川県横浜市港北区樽町　エイピービル") == 1.00
    assert grounding_score(" 神奈川県横浜市港北区樽町 \t エイピービル\n", _RECIPIENT_QUOTE) == 1.00
    # 英数字どうしの間は空白の種類・長さだけが違うなら一致
    assert grounding_score("Suite 200\nNew York", "Suite 200 New York") == 1.00
    assert grounding_score("A-123　Sample.BLD", "A-123 Sample.BLD") == 1.00


def test_grounding_multiline_partial_stays_partial() -> None:
    """空白を吸収しても、部分一致は 0.7 のまま（完全一致に格上げしない）。"""
    assert grounding_score("樽町エイピービル", _RECIPIENT_QUOTE) == 0.70
    assert grounding_score("東京都新宿区四谷9-9-9", _ISSUER_QUOTE) == 0.70
    # 電話・FAX の混在（既存の挙動: 部分一致 0.7）
    assert grounding_score("03-9999-9999 FAX:03-9999-9999", _PHONE_QUOTE) == 0.70


def test_grounding_multiline_does_not_match_different_value() -> None:
    """数字・文字が違う値は、空白を吸収しても一致させない。"""
    assert grounding_score("東京都新宿区四谷9-9-8サプライビル2F", _ISSUER_QUOTE) == 0.00
    assert grounding_score("神奈川県横浜市港北区樽町エイビービル", _RECIPIENT_QUOTE) == 0.00
    assert grounding_score("神奈川県横浜市港北区綱島エイピービル", _RECIPIENT_QUOTE) == 0.00


def test_grounding_keeps_space_between_alnum() -> None:
    """英数字どうしの間の空白は語の区切りとして残す（除くと別の値と一致してしまう）。"""
    # 数量「12」と単価「500」の 2 span をつないだ値は根拠に無い
    assert grounding_score("12500", "12 500") == 0.00
    assert grounding_score("2500", "12 500") == 0.00  # 部分一致にもしない
    # 番地の直後の階数（ADR-0007 規則 3 と同じ線引き）
    assert grounding_score("1-1-13F", "丸の内1-1-1 3F") == 0.00
    assert grounding_score("A-123Sample.BLD", "A-123 Sample.BLD") == 0.00
    assert grounding_score("03-9999-9999FAX:03-9999-9999", _PHONE_QUOTE) == 0.00
    # 空白を挟んでも、同じ英数字の並びなら従来どおり一致
    assert grounding_score("12 500", "12 500") == 1.00


def test_grounding_multiline_vl_still_capped() -> None:
    """DD-09: VL 由来は空白の吸収で一致しても 0.7 のまま。"""
    assert grounding_score(_RECIPIENT_JOINED, _RECIPIENT_QUOTE, source=SpanSource.VL) == 0.70


def test_grounding_type_conversion_with_multiline_quote() -> None:
    """型変換の 0.85 の段は維持（根拠が複数 span でも）。"""
    assert grounding_score("2020-01-31", "請求日付 令和02年\n01月31日", type_converted=True) == 0.85


def test_ocr_confidence_prefers_char_min() -> None:
    assert ocr_confidence(0.95, [0.99, 0.60, 0.88]) == 0.60
    assert ocr_confidence(0.91, None) == 0.91


# --- 根拠 span 全体の ocr_conf（複数 span の値） --------------------------------


def _span(span_id: int, conf: float, **kw: Any) -> Span:
    return Span(span_id=span_id, page=1, text=f"t{span_id}", conf=conf, bbox=[0, 0, 1, 1], **kw)


def test_evidence_ocr_confidence_takes_min_over_all_spans() -> None:
    """2 行目以降の低い conf も効く（sample2 の宛先住所: span 3 = 0.9713、span 4 = 0.9330）。"""
    assert evidence_ocr_confidence([_span(3, 0.9713), _span(4, 0.9330)]) == 0.9330
    # 並び順（LLM の出力順）に依らない
    assert evidence_ocr_confidence([_span(4, 0.9330), _span(3, 0.9713)]) == 0.9330
    # 1 span は従来どおり
    assert evidence_ocr_confidence([_span(1, 0.91)]) == 0.91


def test_evidence_ocr_confidence_char_confs_per_span() -> None:
    """span ごとに ocr_confidence と同じ扱い（char_confs があればその最小、無ければ行 conf）。"""
    spans = [_span(1, 0.99, char_confs=[0.99, 0.70, 0.95]), _span(2, 0.80)]
    assert evidence_ocr_confidence(spans) == 0.70
    # 行 conf が高くても char_confs の最小を採る。char_confs の無い span は行 conf
    spans = [_span(1, 0.99, char_confs=[0.97, 0.96]), _span(2, 0.93)]
    assert evidence_ocr_confidence(spans) == 0.93
    # 空の char_confs は「無い」扱い（ocr_confidence と同じ）
    assert evidence_ocr_confidence([_span(1, 0.88, char_confs=[])]) == 0.88


def test_evidence_ocr_confidence_zero_without_evidence() -> None:
    """根拠 span が無い・State に見つからない span が混じるときは 0.0。"""
    assert evidence_ocr_confidence([]) == 0.0
    assert evidence_ocr_confidence([None]) == 0.0
    # 先頭以外が見つからなくても 0.0（従来は先頭しか見ず、後ろの欠落を見落とした）
    assert evidence_ocr_confidence([_span(1, 0.95), None]) == 0.0


def test_evidence_source_vl_if_any_span_is_vl() -> None:
    """DD-09: 根拠 span のどれかが VL 由来なら VL（先頭が OCR でも上限を掛ける）。"""
    ocr, vl = _span(1, 0.95), _span(2, 0.95, source=SpanSource.VL)
    assert evidence_source([ocr, vl]) is SpanSource.VL
    assert evidence_source([vl, ocr]) is SpanSource.VL
    assert evidence_source([ocr, ocr]) is SpanSource.OCR
    assert evidence_source([]) is SpanSource.OCR
    assert evidence_source([None, vl]) is SpanSource.VL


def test_compute_confidence_is_min() -> None:
    assert compute_confidence(0.9, 0.7) == 0.7


def test_apply_correction_confidence_dd10() -> None:
    assert apply_correction_confidence(0.9, 0.8, dd10_ok=True) == 0.8
    # DD-10 非適合なら補正 confidence を採らない
    assert apply_correction_confidence(0.9, 0.8, dd10_ok=False) == 0.9


def test_auto_elevate() -> None:
    assert auto_elevate(0.5) == 0.98
    assert auto_elevate(0.99) == 0.99
