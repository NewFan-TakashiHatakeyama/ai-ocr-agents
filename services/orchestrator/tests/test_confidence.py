from newfan_schemas import SpanSource

from newfan_orchestrator.confidence import (
    apply_correction_confidence,
    auto_elevate,
    compute_confidence,
    grounding_score,
    ocr_confidence,
)


def test_grounding_exact_match() -> None:
    assert grounding_score("128000", "128000") == 1.00
    # NFKC 正規化して一致（全角）
    assert grounding_score("128000", "１２８０００") == 1.00


def test_grounding_type_conversion() -> None:
    assert grounding_score("2024-05-01", "令和6年5月1日", type_converted=True) == 0.85


def test_grounding_vl_capped() -> None:
    # DD-09: VL 由来は上限 0.7
    assert grounding_score("128000", "128000", source=SpanSource.VL) == 0.70


def test_grounding_partial() -> None:
    assert grounding_score("128000", "合計 128000 円") == 0.70


def test_grounding_none_without_quote() -> None:
    assert grounding_score("128000", None) == 0.00


def test_ocr_confidence_prefers_char_min() -> None:
    assert ocr_confidence(0.95, [0.99, 0.60, 0.88]) == 0.60
    assert ocr_confidence(0.91, None) == 0.91


def test_compute_confidence_is_min() -> None:
    assert compute_confidence(0.9, 0.7) == 0.7


def test_apply_correction_confidence_dd10() -> None:
    assert apply_correction_confidence(0.9, 0.8, dd10_ok=True) == 0.8
    # DD-10 非適合なら補正 confidence を採らない
    assert apply_correction_confidence(0.9, 0.8, dd10_ok=False) == 0.9


def test_auto_elevate() -> None:
    assert auto_elevate(0.5) == 0.98
    assert auto_elevate(0.99) == 0.99
