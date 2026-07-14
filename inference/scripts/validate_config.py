"""サービング設定の起動時バリデーション（DD-03 / DD-08）。

CI とコンテナ起動時に実行する:
    python inference/scripts/validate_config.py inference/structure/pipeline_config.yaml --lang ja

チェック:
- DD-08: 日本語テナントで PP-OCRv6_tiny（日本語非対応, 49言語）を指定していないか。
- DD-03: OCR 認識/検出モデルが明示固定されているか（既定依存を禁止）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - 実行環境依存
    print("PyYAML が必要です: uv pip install pyyaml", file=sys.stderr)
    raise

# 日本語非対応モデル（DD-08）。PP-OCRv6_tiny は 49 言語で日本語を含まない。
JAPANESE_UNSUPPORTED_MODELS = {"PP-OCRv6_tiny_det", "PP-OCRv6_tiny_rec"}


def _iter_model_names(node: Any) -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "model_name" and isinstance(value, str):
                found.append(value)
            else:
                found.extend(_iter_model_names(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_iter_model_names(item))
    return found


def _find_rec_det_models(node: Any, out: dict[str, list[str]]) -> None:
    if isinstance(node, dict):
        module = node.get("module_name")
        name = node.get("model_name")
        if isinstance(name, str):
            if module == "text_recognition":
                out.setdefault("rec", []).append(name)
            elif module == "text_detection":
                out.setdefault("det", []).append(name)
        for value in node.values():
            _find_rec_det_models(value, out)
    elif isinstance(node, list):
        for item in node:
            _find_rec_det_models(item, out)


def validate(config: dict[str, Any], lang: str) -> list[str]:
    errors: list[str] = []

    if lang == "ja":
        for name in _iter_model_names(config):
            if name in JAPANESE_UNSUPPORTED_MODELS:
                errors.append(
                    f"DD-08 違反: 日本語テナントで日本語非対応モデル {name!r} が指定されている"
                )

    pipeline = config.get("pipeline_name", "")
    if pipeline in ("OCR", "PP-StructureV3"):
        models: dict[str, list[str]] = {}
        _find_rec_det_models(config, models)
        if not models.get("rec"):
            errors.append("DD-03 違反: text_recognition の model_name が明示固定されていない")
        if not models.get("det"):
            errors.append("DD-03 違反: text_detection の model_name が明示固定されていない")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("config", type=Path)
    parser.add_argument("--lang", default="ja")
    args = parser.parse_args()

    config = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    errors = validate(config, args.lang)
    if errors:
        for e in errors:
            print(f"[NG] {e}", file=sys.stderr)
        return 1
    print(f"[OK] {args.config} (lang={args.lang})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
