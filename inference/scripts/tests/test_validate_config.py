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
