"""表題部の切り出し（title_zone_text / title_zone_spans, ADR-0008）の決定論テスト。

固定する契約:
- bbox の**中心**が上端 25% に入る span だけ（辺ではなく中心。境界跨ぎで安定）
- 読み順は「行を上から、行内は左から」（OCR の返却順に依存しない）
- 上限 40 span / 600 文字
- 寸法不明（height None/0）→ 空（呼び出し側はファイル名だけで分類する）
- bbox 無し・壊れた bbox・空文字の span は数えない
"""

from newfan_workflow import (
    TITLE_ZONE_MAX_CHARS,
    TITLE_ZONE_MAX_SPANS,
    title_zone_spans,
    title_zone_text,
)

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
