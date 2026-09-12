"""内容ベース分類（⑦, classify_text）の決定論テスト。"""

from newfan_workflow import (
    build_candidate,
    canonical_doc_type,
    classify_text,
    synonyms_for,
    title_zone_text,
)


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


# ---- 表題部の本文信号（ADR-0008, 2026-09-12）。期待値は実スコアから書く ----

_H = 1400  # ページ高さ。上端 25% = 350px


def _invoice_with_quote_refs_in_items() -> list[dict]:
    """請求書。表題部は「請求書」、明細部（350px より下）が見積を 3 箇所で引用する。"""
    return [
        {"text": "請求書", "bbox": [400, 60, 600, 110]},
        {"text": "株式会社ABC 御中", "bbox": [50, 150, 350, 180]},
        {"text": "請求No. 1234", "bbox": [700, 150, 950, 180]},
        {"text": "2026年8月31日", "bbox": [700, 190, 950, 220]},
        {"text": "見積番号 Q-1 に基づく", "bbox": [50, 600, 400, 630]},
        {"text": "見積金額 ¥100,000", "bbox": [50, 640, 400, 670]},
        {"text": "見積書 No.2 参照", "bbox": [50, 680, 400, 710]},
    ]


def test_明細部の見積語は表題部の切り出しで信号にならない():
    # 本文全体を渡すと quotation=3.0 vs invoice=1.0 → quotation 0.75（ゲート閾値に
    # 達して正当な請求書を halt させる）。表題部だけなら quotation=0 で invoice。
    spans = _invoice_with_quote_refs_in_items()
    full = " ".join(s["text"] for s in spans)
    flipped = classify_text(text=full, filename="scan_001.pdf", candidates=_cands())
    assert flipped.doc_type == "quotation"
    assert flipped.confidence == 0.75

    zone = title_zone_text(spans, page_height=_H)
    assert zone == "請求書 株式会社ABC 御中 請求No. 1234 2026年8月31日"
    out = classify_text(text=zone, filename="scan_001.pdf", candidates=_cands())
    assert out.doc_type == "invoice"
    assert out.scores == {"invoice": 1.0, "quotation": 0.0, "purchase_order": 0.0}
    assert out.confidence == 0.667  # 本文 1 領域: margin 1.0 × (0.5 + 0.5 × 1/3)
    assert out.evidence == {"filename": 0, "text": 1}
    assert out.reason == "「請求書」が表題部に一致"


def test_表題が食い違ってもファイル名1語が勝つ_確信度は下がる():
    # ファイル名は請求書（3.0）、表題は「御見積書」（御見積/見積書/見積 が重なって 1 領域 = 1.0）。
    # quotation は 0 → 1.0 に上がるが、invoice 3.0 が勝つ。確信度は 1.0 → 0.75。
    title = title_zone_text([{"text": "御見積書", "bbox": [400, 60, 600, 110]}], page_height=_H)
    base = classify_text(text="", filename="請求書_ABC商事_2026.pdf", candidates=_cands())
    assert (base.doc_type, base.confidence) == ("invoice", 1.0)
    out = classify_text(text=title, filename="請求書_ABC商事_2026.pdf", candidates=_cands())
    assert out.doc_type == "invoice"
    assert out.scores == {"invoice": 3.0, "quotation": 1.0, "purchase_order": 0.0}
    assert out.confidence == 0.75  # margin 3/4 × evidence 飽和
    assert out.evidence == {"filename": 1, "text": 1}
    assert out.reason == "「請求書」がファイル名に一致"


def test_表題が2領域で食い違えばファイル名の確信度はゲート閾値を割る():
    # 「御見積書」＋「見積金額」= quotation 2.0 vs invoice 3.0 → margin 0.6。
    # 種別は反転しない（ファイル名優位）が、0.75 未満なのでゲートはスキーマ種別へ倒れる。
    title = title_zone_text(
        [{"text": "御見積書", "bbox": [400, 60, 600, 110]},
         {"text": "見積金額 ¥1", "bbox": [700, 200, 900, 230]}],
        page_height=_H,
    )
    out = classify_text(text=title, filename="請求書_ABC商事_2026.pdf", candidates=_cands())
    assert out.doc_type == "invoice"
    assert out.confidence == 0.6
    gated = classify_text(
        text=title, filename="請求書_ABC商事_2026.pdf", candidates=_cands(), min_confidence=0.75
    )
    assert gated.doc_type is None


def test_表題3領域とファイル名1語は同点_同点はファイル名側_候補順に依らない():
    # 本文は領域 3 で飽和（3.0）＝ファイル名 1 語（3.0）。本文はファイル名を上回れない。
    title = title_zone_text(
        [{"text": "御見積書", "bbox": [400, 60, 600, 110]},
         {"text": "見積金額 ¥1", "bbox": [700, 200, 900, 230]},
         {"text": "見積番号 Q-9", "bbox": [700, 240, 900, 270]}],
        page_height=_H,
    )
    for cands in (_cands(), list(reversed(_cands()))):
        out = classify_text(text=title, filename="請求書_ABC商事_2026.pdf", candidates=cands)
        assert out.doc_type == "invoice"
        assert out.scores["invoice"] == 3.0 and out.scores["quotation"] == 3.0
        assert out.confidence == 0.5


def test_ファイル名に手がかりが無くても表題で当てる():
    title = title_zone_text([{"text": "御請求書", "bbox": [400, 60, 600, 110]}], page_height=_H)
    out = classify_text(text=title, filename="scan_0001.pdf", candidates=_cands())
    assert out.doc_type == "invoice"
    assert out.confidence == 0.667
    assert out.evidence == {"filename": 0, "text": 1}
    assert out.reason == "「請求書・御請求」が表題部に一致"


def test_手がかりなしのevidenceは0():
    out = classify_text(text="", filename="a.pdf", candidates=_cands())
    assert out.evidence == {"filename": 0, "text": 0}
