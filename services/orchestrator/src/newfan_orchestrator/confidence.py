"""confidence / grounding 算出（詳細設計 §5.7.2）。

初期版（PoC で係数較正）。LLM 非依存の純ロジックのため単体テスト対象。
"""

from __future__ import annotations

import unicodedata
from typing import Optional

from newfan_schemas import SpanSource

# grounding スコア（§5.7.2）
GROUNDING_EXACT = 1.00  # value_normalized が source_quote の正規化文字列と一致
GROUNDING_TYPE_CONV = 0.85  # 型変換のみで導出可能（和暦→西暦 等）
GROUNDING_VL_OR_PARTIAL = 0.70  # VL 由来 / 部分一致（DD-09 上限）
GROUNDING_NONE = 0.00  # 根拠 span なし → 強制レビュー

AUTO_ELEVATION = 0.98  # 検証合格時の昇格値


def _norm(text: str) -> str:
    return unicodedata.normalize("NFKC", text).strip()


def grounding_score(
    value_normalized: Optional[str],
    source_quote: Optional[str],
    *,
    source: SpanSource = SpanSource.OCR,
    type_converted: bool = False,
) -> float:
    """value と原文根拠の対応度から grounding を返す。"""
    if not source_quote or value_normalized is None:
        return GROUNDING_NONE
    if source is SpanSource.VL:
        return GROUNDING_VL_OR_PARTIAL  # DD-09: VL 由来は上限 0.7
    if _norm(value_normalized) == _norm(source_quote):
        return GROUNDING_EXACT
    if type_converted:
        return GROUNDING_TYPE_CONV
    if _norm(value_normalized) in _norm(source_quote):
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
