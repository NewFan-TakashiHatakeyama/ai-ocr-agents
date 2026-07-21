import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from validate_config import validate  # noqa: E402


def _structure_config() -> dict:
    return {
        "pipeline_name": "PP-StructureV3",
        "SubPipelines": {
            "GeneralOCR": {
                "SubModules": {
                    "TextDetection": {
                        "module_name": "text_detection",
                        "model_name": "PP-OCRv6_medium_det",
                    },
                    "TextRecognition": {
                        "module_name": "text_recognition",
                        "model_name": "PP-OCRv6_medium_rec",
                    },
                }
            }
        },
    }


def test_valid_structure_passes() -> None:
    assert validate(_structure_config(), "ja") == []


def test_dd08_tiny_japanese_blocked() -> None:
    cfg = _structure_config()
    cfg["SubPipelines"]["GeneralOCR"]["SubModules"]["TextRecognition"][
        "model_name"
    ] = "PP-OCRv6_tiny_rec"
    errors = validate(cfg, "ja")
    assert any("DD-08" in e for e in errors)


def test_dd08_small_medium_allowed_for_japanese() -> None:
    """small/medium は日本語可（実測: かな180文字）。DD-08 のブロック対象は tiny のみ。"""
    for tier in ("small", "medium"):
        cfg = _structure_config()
        subs = cfg["SubPipelines"]["GeneralOCR"]["SubModules"]
        subs["TextRecognition"]["model_name"] = f"PP-OCRv6_{tier}_rec"
        subs["TextDetection"]["model_name"] = f"PP-OCRv6_{tier}_det"
        assert validate(cfg, "ja") == [], f"{tier} は日本語テナントで許可される"


def test_dd08_tiny_allowed_for_non_japanese() -> None:
    cfg = _structure_config()
    cfg["SubPipelines"]["GeneralOCR"]["SubModules"]["TextRecognition"][
        "model_name"
    ] = "PP-OCRv6_tiny_rec"
    cfg["SubPipelines"]["GeneralOCR"]["SubModules"]["TextDetection"][
        "model_name"
    ] = "PP-OCRv6_tiny_det"
    # 英語等では tiny も許可（DD-08 は日本語限定）
    assert validate(cfg, "en") == []


def test_dd03_missing_rec_model_flagged() -> None:
    cfg = {"pipeline_name": "OCR", "SubModules": {}}
    errors = validate(cfg, "ja")
    assert any("DD-03" in e for e in errors)


def _with_layout(model: str) -> dict:
    cfg = _structure_config()
    cfg["SubModules"] = {
        "LayoutDetection": {"module_name": "layout_detection", "model_name": model}
    }
    return cfg


def test_nonexistent_model_flagged(monkeypatch) -> None:
    """paddlex に実在しないモデル名（例: PP-DocLayoutV3）を弾く。"""
    import validate_config

    monkeypatch.setattr(
        validate_config,
        "paddlex_models",
        lambda m: {"PP-DocLayout_plus-L", "PP-DocLayoutV2"} if m == "layout_detection" else None,
    )
    errors = validate_config.validate(_with_layout("PP-DocLayoutV3"), "ja")
    assert any("実在しない" in e and "PP-DocLayoutV3" in e for e in errors)


def test_existing_model_passes(monkeypatch) -> None:
    import validate_config

    monkeypatch.setattr(
        validate_config,
        "paddlex_models",
        lambda m: {"PP-DocLayout_plus-L"} if m == "layout_detection" else None,
    )
    assert validate_config.validate(_with_layout("PP-DocLayout_plus-L"), "ja") == []


def test_existence_check_skipped_without_paddlex(monkeypatch) -> None:
    """paddlex 未導入（CI 等）では実在チェックはスキップし、他の検査は動く。"""
    import validate_config

    monkeypatch.setattr(validate_config, "paddlex_models", lambda m: None)
    assert validate_config.validate(_with_layout("PP-DocLayoutV3"), "ja") == []
