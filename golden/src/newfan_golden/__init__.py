"""ゴールデンセット精度回帰（§14.2 / DD-03）。本番昇格のゲート。"""

from newfan_golden.dataset import GoldenDoc, GoldenFormatError, load_jsonl
from newfan_golden.metrics import (
    HARMFUL_RATE_LIMIT,
    REGRESSION_LIMIT_PT,
    GateResult,
    GoldField,
    PredField,
    Report,
    check_gate,
    evaluate,
    score_document,
)

__all__ = [
    "GoldenDoc",
    "GoldenFormatError",
    "load_jsonl",
    "GoldField",
    "PredField",
    "Report",
    "GateResult",
    "evaluate",
    "score_document",
    "check_gate",
    "HARMFUL_RATE_LIMIT",
    "REGRESSION_LIMIT_PT",
]
