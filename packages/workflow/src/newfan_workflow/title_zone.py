"""表題部（1 ページ目の上端）の OCR span から、帳票分類（⑦）へ渡す本文信号を切り出す。

純関数。DB も HTTP も LLM も持たない。gateway（POST /documents/{id}/classify）と
orchestrator（process.classify ゲート）が**同じ関数で同じ文字列**を作り、それを
classify_text の text に渡す（ファイル名は従来どおり ×3、表題部は ×1）。

なぜ本文全体ではなく表題部だけか（ADR-0008）:

- 帳票の種別は表題（「請求書」「御見積書」「納品書」）として印字され、それは
  ほぼ例外なく 1 ページ目の上端にある。
- 一方、明細や備考には**他種別の語が常態的に出る**（請求書の明細が「見積No」を
  引用する、納品書の備考が「請求書は別途送付」と書く）。本文全体を信号にすると
  この構造的な偏りが正当な run を halt させる。抽出値を信号にしない判断
  （2026-08-17 の敵対的レビューで実測確定）と同じ根拠であり、それを本文経由で
  再導入しないための切り出しである。
- 上端 25% は A4 縦で約 74mm。宛名・表題・番号・日付・発行者の帯に収まり、
  明細表のヘッダ行（品名/数量/単価）が始まる前で切れる。

寸法が分からないページでは何も返さない（fail-open）。``pages.height`` は DDL で
nullable であり、寸法不明のまま「上端」を決めることはできない。空文字を返せば
呼び出し側は従来どおりファイル名だけで分類する（除外領域の適用と同じ方針）。
"""

from __future__ import annotations

import statistics
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

# ページ高さに対する表題部の割合。A4 縦（297mm）で約 74mm。
TITLE_ZONE_RATIO = 0.25
# 表題部から取る span の上限と、連結後の文字数の上限。表題部は数十 span で足り、
# 上限は「宛名ラベルの中に明細表が入り込む」ような異常レイアウトで本文が
# 際限なく信号化しないための飽和。
TITLE_ZONE_MAX_SPANS = 40
TITLE_ZONE_MAX_CHARS = 600


@dataclass(frozen=True)
class _Item:
    cy: float
    cx: float
    h: float
    text: str


def _bbox_of(span: Mapping[str, Any]) -> tuple[float, float, float, float] | None:
    bbox = span.get("bbox")
    if not isinstance(bbox, (list, tuple)) or len(bbox) < 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in bbox[:4])
    except (TypeError, ValueError):
        return None
    return x0, y0, x1, y1


def title_zone_spans(
    spans: Iterable[Mapping[str, Any]],
    *,
    page_height: int | float | None,
    zone_ratio: float = TITLE_ZONE_RATIO,
    max_spans: int = TITLE_ZONE_MAX_SPANS,
) -> list[str]:
    """表題部に入る span の原文を読み順で返す（上限 max_spans 件）。

    span は run_spans の 1 要素と同じ形（``{"text": str, "bbox": [x0,y0,x1,y1] | None}``）。
    判定は **bbox の中心が上端 ``zone_ratio`` に入るか**。境界を跨ぐ span で
    不安定にならないよう、辺ではなく中心点で見る（位置ガードと同じ）。

    - 寸法不明（``page_height`` が None / 0 以下）→ 空（呼び出し側はファイル名のみ）
    - bbox が無い・壊れている span、空文字の span は数えない
    - 読み順は「行を上から、行内は左から」。行のまとめは中心 y の差が
      span 高さの中央値の半分以内なら同じ行とみなす（OCR の返却順に依存しない）
    """
    if page_height is None or page_height <= 0:
        return []
    limit = float(page_height) * zone_ratio
    items: list[_Item] = []
    for s in spans:
        text = str(s.get("text") or "").strip()
        if not text:
            continue
        bb = _bbox_of(s)
        if bb is None:
            continue
        x0, y0, x1, y1 = bb
        cy = (y0 + y1) / 2.0
        if cy > limit:
            continue
        items.append(_Item(cy=cy, cx=(x0 + x1) / 2.0, h=abs(y1 - y0), text=text))
    if not items:
        return []

    # 読み順: 行にまとめてから行内を左から。行の基準は行の先頭（最上）の span で、
    # 許容幅は高さの中央値の半分（同じ行の文字は中心 y がほぼ揃う）。
    items.sort(key=lambda it: (it.cy, it.cx))
    tol = statistics.median(it.h for it in items) * 0.5
    rows: list[list[_Item]] = []
    for it in items:
        if rows and it.cy - rows[-1][0].cy <= tol:
            rows[-1].append(it)
        else:
            rows.append([it])
    ordered = [it for row in rows for it in sorted(row, key=lambda it: it.cx)]
    return [it.text for it in ordered[:max_spans]]


def title_zone_text(
    spans: Iterable[Mapping[str, Any]],
    *,
    page_height: int | float | None,
    zone_ratio: float = TITLE_ZONE_RATIO,
    max_spans: int = TITLE_ZONE_MAX_SPANS,
    max_chars: int = TITLE_ZONE_MAX_CHARS,
) -> str:
    """表題部の span を空白で連結した文字列（classify_text の text に渡す形）。

    上限 ``max_chars`` で切る。切れ目が**英数語の途中**に掛かったときは、その語の
    断片を落とす。classify_text の英数語彙は語境界付き（``order`` が ``border`` に
    埋没ヒットしない）なので、``orderly`` を ``order|ly`` で切ると元の本文には無い
    一致が生まれる（``orderly`` → purchase_order 1 領域）。日本語の語彙は境界を
    要求しないため、切れて残った前半（「見積書」→「見積」）が一致するなら元の語も
    同じ候補に一致しており、誤った一致にはならない。
    """
    joined = " ".join(
        title_zone_spans(
            spans, page_height=page_height, zone_ratio=zone_ratio, max_spans=max_spans
        )
    )
    if len(joined) <= max_chars:
        return joined
    cut = joined[:max_chars]
    if _is_word_char(joined[max_chars]) and _is_word_char(cut[-1]):
        end = len(cut)
        while end > 0 and _is_word_char(cut[end - 1]):
            end -= 1
        cut = cut[:end]
    return cut


def _is_word_char(ch: str) -> bool:
    """classify_text の英数語彙が「語の内側」と見なす文字か（NFKC 後の [a-z0-9]）。

    全角英数（「ｏｒｄｅｒ」）も NFKC で ASCII になるので同じ扱い。それ以外
    （かな・漢字・記号・空白）は語境界。
    """
    n = unicodedata.normalize("NFKC", ch)
    return bool(n) and n.isascii() and n.isalnum()
