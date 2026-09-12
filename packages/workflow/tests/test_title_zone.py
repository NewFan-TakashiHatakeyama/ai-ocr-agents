"""表題部の切り出し（title_zone_text / title_zone_spans, ADR-0008）の決定論テスト。

固定する契約:
- bbox の**中心**が上端 25% に入る span だけ（辺ではなく中心。境界跨ぎで安定）
- 読み順は「行を上から、行内は左から」（OCR の返却順に依存しない）
- 上限 40 span / 600 文字。文字数の切れ目が英数語の途中なら、その断片は落とす
  （"orderly" → "order" の誤一致を作らない）
- 寸法不明（height None/0）→ 空（呼び出し側はファイル名だけで分類する）
- bbox 無し・壊れた bbox・空文字の span は数えない
"""

from newfan_workflow import (
    TITLE_ZONE_MAX_CHARS,
    TITLE_ZONE_MAX_SPANS,
    build_candidate,
    classify_text,
    title_zone_spans,
    title_zone_text,
)
from newfan_workflow.classify import DOC_TYPE_SYNONYMS

H = 1400  # FakeRasterizer と同じ A4 相当（上端 25% = 350px 未満）


def _span(text: str, y0: int, y1: int, x0: int = 100, x1: int = 300) -> dict:
    return {"span_id": 0, "text": text, "bbox": [x0, y0, x1, y1], "conf": 0.9}


def test_上端25パーセントに中心がある_spanだけを取る():
    spans = [
        _span("請求書", 60, 110),            # 中心 85 → 入る
        _span("境界上", 330, 370),           # 中心 350 = 境界 → 入る（<=）
        _span("境界下", 331, 371),           # 中心 351 → 入らない
        _span("見積番号 Q-1", 600, 630),     # 明細部 → 入らない
    ]
    assert title_zone_spans(spans, page_height=H) == ["請求書", "境界上"]


def test_読み順は行を上から行内は左から():
    # OCR の返却順は右→左・下→上に入れ替えてある
    spans = [
        _span("日付", 150, 180, x0=700, x1=950),
        _span("御中", 150, 182, x0=300, x1=400),   # 同じ行（中心 y の差 1px）
        _span("株式会社ABC", 152, 180, x0=50, x1=290),
        _span("請求書", 60, 110, x0=400, x1=600),   # 上の行
    ]
    assert title_zone_spans(spans, page_height=H) == ["請求書", "株式会社ABC", "御中", "日付"]
    assert title_zone_text(spans, page_height=H) == "請求書 株式会社ABC 御中 日付"


def test_span数の上限で飽和する():
    spans = [_span(f"w{i}", 10 + i * 5, 20 + i * 5) for i in range(TITLE_ZONE_MAX_SPANS + 10)]
    got = title_zone_spans(spans, page_height=H)
    assert len(got) == TITLE_ZONE_MAX_SPANS
    assert got[0] == "w0"  # 上から数える（後ろが切れる）


def test_文字数の上限で切る():
    spans = [_span("あ" * 500, 10, 30), _span("い" * 500, 40, 60)]
    got = title_zone_text(spans, page_height=H)
    assert len(got) == TITLE_ZONE_MAX_CHARS
    assert got.startswith("あ")


def _all_cands():
    return [build_candidate(dt) for dt in DOC_TYPE_SYNONYMS]


def test_文字数の切れ目が英数語の途中なら断片を落とす():
    # 594 文字 + 空白 + "orderly" = 602 文字。600 で切ると "order|ly" になり、語境界
    # 付きの "order" が purchase_order に 1 領域で一致する（元の本文では一致しない。
    # レビューで実測: 0.667 のサジェストに「order」が表題部に一致 と出た）。
    spans = [_span("あ" * 594, 10, 30), _span("orderly", 40, 60)]
    full = " ".join(s["text"] for s in spans)
    assert len(full) == TITLE_ZONE_MAX_CHARS + 2
    assert classify_text(text=full, filename="0001.pdf", candidates=_all_cands()).doc_type is None

    got = title_zone_text(spans, page_height=H)
    assert got == "あ" * 594 + " "  # 断片 "order" を落とす
    assert classify_text(text=got, filename="0001.pdf", candidates=_all_cands()).doc_type is None


def test_文字数の切れ目が英数語の途中でも全角英数は同じ扱い():
    # NFKC で ASCII になる全角「ｏｒｄｅｒｌｙ」も classify_text では "orderly" なので、
    # 切れ目の判定も同じ（断片を落とす）
    spans = [_span("あ" * 594, 10, 30), _span("ｏｒｄｅｒｌｙ", 40, 60)]
    assert title_zone_text(spans, page_height=H) == "あ" * 594 + " "


def test_文字数の切れ目が語境界なら切った位置をそのまま使う():
    # "order" の直後が空白（語境界）で切れる → 元の本文でも "order" は一致するので落とさない
    spans = [_span("あ" * 594, 10, 30), _span("order", 40, 60), _span("x", 70, 90)]
    got = title_zone_text(spans, page_height=H)
    assert got == "あ" * 594 + " order"
    assert len(got) == TITLE_ZONE_MAX_CHARS


def test_寸法不明なら空_ファイル名のみに倒す():
    spans = [_span("請求書", 60, 110)]
    assert title_zone_text(spans, page_height=None) == ""
    assert title_zone_text(spans, page_height=0) == ""
    assert title_zone_text(spans, page_height=-1) == ""


def test_bbox無し_壊れたbbox_空文字は数えない():
    spans = [
        {"span_id": 1, "text": "御請求書", "bbox": None, "conf": 0.8},
        {"span_id": 2, "text": "御請求書", "bbox": [1, 2], "conf": 0.8},
        {"span_id": 3, "text": "御請求書", "bbox": ["x", 0, 0, 0], "conf": 0.8},
        {"span_id": 4, "text": "   ", "bbox": [10, 10, 50, 30], "conf": 0.8},
        {"span_id": 5, "text": None, "bbox": [10, 10, 50, 30], "conf": 0.8},
        {"span_id": 6, "text": "納品書", "bbox": [10, 10, 50, 30], "conf": 0.8},
    ]
    assert title_zone_spans(spans, page_height=H) == ["納品書"]


def test_spanが無ければ空():
    assert title_zone_text([], page_height=H) == ""
