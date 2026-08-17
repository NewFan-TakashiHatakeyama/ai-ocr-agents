"""内容ベース分類（⑦, classify_text）の決定論テスト。"""

from newfan_workflow import build_candidate, canonical_doc_type, classify_text, synonyms_for


def _cands():
    return [
        build_candidate("invoice", ["取引先名", "請求番号", "お支払期限"]),
        build_candidate("quotation", ["御見積合計金額", "見積番号"]),
        build_candidate("purchase_order", ["発注番号"]),
    ]


def test_ファイル名で見積書を当てる():
    out = classify_text(text="", filename="見積_ABC商事_2026.pdf", candidates=_cands())
    assert out.doc_type == "quotation"
    assert out.confidence > 0.5


def test_本文の代表語で請求書を当てる():
    text = "請求書\nAAA食品株式会社 御中\n請求金額 ¥136,998\nお支払期限 2026-04-30"
    out = classify_text(text=text, filename="scan_0001.pdf", candidates=_cands())
    assert out.doc_type == "invoice"


def test_ファイル名は本文より優先される():
    # 本文は請求語彙、ファイル名は発注 → ファイル名重みで発注が勝つ
    out = classify_text(
        text="請求金額 合計", filename="発注書_0001.pdf", candidates=_cands()
    )
    assert out.doc_type == "purchase_order"


def test_手がかりなしはNoneを返す():
    out = classify_text(text="", filename="a.pdf", candidates=_cands())
    assert out.doc_type is None
    assert out.confidence == 0.0


def test_min_confidence未満はNoneに倒す():
    # 曖昧（拮抗）なケースは既定にフォールバックできるよう None
    cands = [build_candidate("invoice", ["共通語"]), build_candidate("quotation", ["共通語"])]
    out = classify_text(text="共通語", filename="", candidates=cands, min_confidence=0.6)
    assert out.doc_type is None


def test_synonyms_forは既知の別名を含む():
    assert "見積書" in synonyms_for("quotation")
    assert "請求書" in synonyms_for("invoice")
    # 未知の doc_type は名前そのものだけ
    assert synonyms_for("weird_type") == ["weird_type"]


def test_ストップワードは語彙に入らない():
    cand = build_candidate("invoice", ["合計", "金額", "取引先名"])
    assert "合計" not in cand.keywords
    assert "取引先名" in cand.keywords


# ---- 敵対的レビュー確定所見の回帰（2026-08-17） ----


def test_包含語は同一箇所を二重加点しない():
    # 「見積書」1回の出現が「見積書」「見積」の2ヒット＝score2.0 になり、
    # ゲート閾値0.75を超えて正当な run を halt させた（確定major）。領域マージで1証拠に。
    out = classify_text(
        text="見積書No.Q-1に基づく", filename="scan_001.pdf", candidates=_cands()
    )
    assert out.scores["quotation"] == 1.0  # 2.0 ではない
    assert out.confidence < 0.75  # ゲート閾値未満（呼び出し側でスキーマへフォールバック）


def test_英数語は語境界を要求する():
    # "order"⊂"Border" の埋没ヒットが confidence 1.0 を出していた（確定major）
    out = classify_text(text="", filename="Border_Inc_2026.pdf", candidates=_cands())
    assert out.doc_type is None
    assert out.scores["purchase_order"] == 0.0


def test_語境界があっても正当な英数語は当たる():
    out = classify_text(text="", filename="purchase_order_2026.pdf", candidates=_cands())
    assert out.doc_type == "purchase_order"


def test_NFKCで全角英数と半角カナを取りこぼさない():
    # 全角「ＩＮＶＯＩＣＥ」・半角カナ「ﾚｼｰﾄ」が一切マッチしなかった（確定minor）
    out = classify_text(text="", filename="ＩＮＶＯＩＣＥ＿２０２６.pdf", candidates=_cands())
    assert out.doc_type == "invoice"
    cands = [build_candidate("receipt")]
    out2 = classify_text(text="", filename="ﾚｼｰﾄ_20260401.pdf", candidates=cands)
    assert out2.doc_type == "receipt"


def test_canonical_doc_typeは日本語名を正準キーへ解決する():
    assert canonical_doc_type("請求書") == "invoice"
    assert canonical_doc_type("invoice") == "invoice"
    assert canonical_doc_type("見積書") == "quotation"
    assert canonical_doc_type("weird_type") == "weird_type"
    assert canonical_doc_type(None) == ""


def test_synonyms_forは日本語名にも正準語彙一式を与える():
    # 日本語名 doc_type の候補が英語正準キー候補に語彙量で敗けないように（確定major対策）
    words = synonyms_for("請求書")
    assert "invoice" in words
    assert "御請求" in words
