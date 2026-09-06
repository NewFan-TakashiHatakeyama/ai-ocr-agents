"""正解値の位置探索（人が矩形を引く操作の再現）。

領域を「抽出 AI が返した bbox」から起こすと、AI が取り違えた項目では領域まで
間違う。実測でビズリフォーム A4 の 3 枚中 2 枚がそうなった（発行元として紙面左＝
実際は宛先 を返し、そこから「発行元＝左」というテンプレートができた）。
ここが壊れると Phase 4 の計測そのものが別物を測る。
"""

from __future__ import annotations

from typing import Any

from newfan_golden.region_from_gold import key, locate


class _Span:
    def __init__(self, text: str, bbox: list[float]) -> None:
        self.text = text
        self.bbox = bbox


def _spans(*rows: tuple[str, list[float]]) -> list[Any]:
    return [_Span(t, b) for t, b in rows]


def test_1つの_span_に収まる値を見つける() -> None:
    spans = _spans(
        ("株式会社エイビーエム", [100, 200, 300, 220]),
        ("わくわく物産株式会社", [500, 140, 700, 160]),
    )
    bbox, n = locate(spans, "わくわく物産株式会社")
    assert bbox == [500.0, 140.0, 700.0, 160.0]
    assert n == 1


def test_複数の_span_に割れた住所を連結して見つける() -> None:
    """住所は 2〜3 span に割れる。1 span ずつ見ていると永久に一致しない。"""
    spans = _spans(
        ("東京都新宿区四谷9-9-9", [500, 200, 700, 220]),
        ("サプライビル2F", [500, 222, 640, 242]),
    )
    bbox, _ = locate(spans, "東京都新宿区四谷9－9－9サプライビル2F")
    assert bbox == [500.0, 200.0, 700.0, 242.0]


def test_表記の揺れを越えて見つける() -> None:
    spans = _spans(("￥395,217-", [200, 400, 340, 430]))
    bbox, _ = locate(spans, "395217")
    assert bbox == [200.0, 400.0, 340.0, 430.0]


def test_同じ値が複数箇所にあれば候補数を返す() -> None:
    """合計金額は上部の枠と明細末尾の 2 箇所に出ることが多い。

    人でも一意に引けないので、候補数を記録して解釈に使う（黙って 1 つ選ばない）。
    """
    spans = _spans(
        ("58,300", [600, 300, 700, 320]),
        ("合計", [300, 900, 360, 920]),
        ("58,300", [600, 900, 700, 920]),
    )
    bbox, n = locate(spans, "58300")
    assert n == 2
    assert bbox == [600.0, 300.0, 700.0, 320.0]  # 読み順で先に出た方


def test_見つからなければ領域を作らない() -> None:
    """OCR が文字を拾えていない項目は、どんなヒントを渡しても当てられない。"""
    spans = _spans(("まったく別の文字列", [0, 0, 10, 10]))
    bbox, n = locate(spans, "わくわく物産株式会社")
    assert bbox is None
    assert n == 0


def test_空の正解値は探さない() -> None:
    assert locate(_spans(("なにか", [0, 0, 1, 1])), "") == (None, 0)


def test_探索キーは敬称と記号を落とす() -> None:
    assert key("大熊 和一 様") == key("大熊和一")
    assert key("￥395,217-") == key("395217")
    assert key("株式会社山田製作所 御中") == key("株式会社山田製作所")
