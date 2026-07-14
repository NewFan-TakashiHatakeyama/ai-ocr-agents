from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class NormContext:
    """正規化の文脈。年省略時の補完（§5.6 date）に使う。"""

    context_year: Optional[int] = None


@dataclass
class NormalizationResult:
    """正規化結果。

    - value: value_normalized（正規化後の文字列）。導出不能なら None。
    - type_converted: 型変換で導出したか（grounding 0.85 判定に使う, §5.7.2）。
    - confidence_cap: confidence 上限（date の年補完時 0.85 等）。
    - needs_review_hint: 自動変換せず LLM 補正へ回す候補理由（DD-10）。
    - extra: 構造化結果（bank 口座分解、tax {rate, reduced_flag} 等）。
    """

    value: Optional[str]
    type_converted: bool = False
    confidence_cap: Optional[float] = None
    needs_review_hint: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)
