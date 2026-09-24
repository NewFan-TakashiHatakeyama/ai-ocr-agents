"""confidence / grounding 算出（詳細設計 §5.7.2）。

初期版（PoC で係数較正）。LLM 非依存の純ロジックのため単体テスト対象。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional

from newfan_schemas import SpanSource

# grounding スコア（§5.7.2）
GROUNDING_EXACT = 1.00  # value_normalized が source_quote の正規化文字列と一致
GROUNDING_TYPE_CONV = 0.85  # 型変換のみで導出可能（和暦→西暦 等）
GROUNDING_VL_OR_PARTIAL = 0.70  # VL 由来 / 部分一致（DD-09 上限）
GROUNDING_NONE = 0.00  # 根拠 span なし → 強制レビュー

AUTO_ELEVATION = 0.98  # 検証合格時の昇格値

# 空白類の連なり（改行・タブ・全角空白を含む。全角空白は NFKC で半角空白になる）
_WS = re.compile(r"\s+")
# 空白を語の区切りとして残す文字。NFKC 後に判定するので全角英数字もここに入る
_WORD_CHAR = re.compile(r"[0-9A-Za-z]")


def _norm(text: str) -> str:
    """grounding の比較形。NFKC のうえで空白類の違いを吸収する。

    空白類の連なりは、**両隣が半角英数字のときだけ** 1 つの半角空白に畳み、それ以外
    （日本語の文字・記号に接するもの、先頭・末尾）は除く。

    source_quote は根拠 span のテキストを半角空白で連結したもの（kie）で、この空白は
    原文に無い。複数行にまたがる値（住所など）を LLM が行を**つないで**返すと
    （「…樽町エイピービル」）、根拠は「…樽町 エイピービル」になり、NFKC + strip だけの
    比較では一致も部分一致もせず grounding 0（確信度 0.00・強制レビュー）に落ちていた。
    LLM が改行で区切って返したときだけ string の正規化が改行を空白に畳んで偶然一致していた。

    英数字どうしの間の空白を残すのは、除くと別の値と一致してしまうため ── 根拠
    「12 500」（数量と単価の 2 span）に値「12500」が、「1-1-1 3F」に「1-1-13F」が
    一致してしまう。住所の正規化（ADR-0007 規則 3）と同じ線引きにしてあるので、
    address_jp が空白を除いた値も、同じ規則で畳んだ根拠と比べれば一致する。
    """
    s = unicodedata.normalize("NFKC", text)

    def repl(m: re.Match[str]) -> str:
        i, j = m.start(), m.end()
        if i > 0 and j < len(s) and _WORD_CHAR.match(s[i - 1]) and _WORD_CHAR.match(s[j]):
            return " "
        return ""

    return _WS.sub(repl, s)


def grounding_score(
    value_normalized: Optional[str],
    source_quote: Optional[str],
    *,
    source: SpanSource = SpanSource.OCR,
    type_converted: bool = False,
) -> float:
    """value と原文根拠の対応度から grounding を返す。

    一致・部分一致は ``_norm``（NFKC + 空白類の違いの吸収）の形で比べる。
    """
    if not source_quote or value_normalized is None:
        return GROUNDING_NONE
    if source is SpanSource.VL:
        return GROUNDING_VL_OR_PARTIAL  # DD-09: VL 由来は上限 0.7
    value = _norm(value_normalized)
    quote = _norm(source_quote)
    if value == quote:
        return GROUNDING_EXACT
    if type_converted:
        return GROUNDING_TYPE_CONV
    if value in quote:
        return GROUNDING_VL_OR_PARTIAL
    return GROUNDING_NONE


def ocr_confidence(line_conf: float, char_confs: Optional[list[float]]) -> float:
    """char_confs があれば最小値、無ければ行 conf。"""
    if char_confs:
        return min(char_confs)
    return line_conf


def compute_confidence(ocr_conf: float, grounding: float) -> float:
    """confidence = min(ocr_conf, grounding)。"""
    return min(ocr_conf, grounding)


def apply_correction_confidence(
    confidence: float, correction_confidence: float, *, dd10_ok: bool
) -> float:
    """補正適用時は min(現confidence, llm_correct.confidence)（DD-10 適合時のみ）。"""
    if not dd10_ok:
        return confidence
    return min(confidence, correction_confidence)


def auto_elevate(confidence: float) -> float:
    """決定論バリデーション合格フィールドを昇格（§5.7.2）。"""
    return max(confidence, AUTO_ELEVATION)
