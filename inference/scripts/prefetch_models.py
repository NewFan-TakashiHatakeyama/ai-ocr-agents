"""設定ファイルが参照するモデルをすべて事前取得する（イメージへ焼き込む用）。

Fargate はタスク再作成のたびにコンテナが新規になるため、モデル（数百MB）を起動時に
ダウンロードするとコールドスタートが伸びる。ビルド時に取得してイメージへ含める。

    python inference/scripts/prefetch_models.py inference/structure/pipeline_config.yaml ...

設定内の (module_name, model_name) を走査して paddlex の create_model で取得する。
**無効化した機能のモデルは取得しない**（完全 config には use_* が False でも SubModule 定義が
残っているため、素直に走査すると使わないモデルまで焼いてイメージが太る。実測: chart の
PP-Chart2Table 2.1GB / formula の PP-FormulaNet_plus-L 0.7GB＝計 2.9GB が無駄だった）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover
    print("PyYAML が必要です", file=sys.stderr)
    raise

# セクション名 → それを有効化する use_* フラグ。False のセクションは丸ごと取得しない。
GATES: dict[str, str] = {
    "ChartRecognition": "use_chart_recognition",
    "FormulaRecognition": "use_formula_recognition",
    "DocPreprocessor": "use_doc_preprocessor",
    "SealRecognition": "use_seal_recognition",
    "TableRecognition": "use_table_recognition",
    "RegionDetection": "use_region_detection",
    "TextLineOrientation": "use_textline_orientation",
}


def _enabled(root: dict[str, Any], section: str) -> bool:
    gate = GATES.get(section)
    if gate is None:
        return True
    return root.get(gate) is not False  # 未指定は既定有効とみなす


def _iter_models(node: Any, out: list[str], root: dict[str, Any]) -> None:
    if isinstance(node, dict):
        name = node.get("model_name")
        if isinstance(node.get("module_name"), str) and isinstance(name, str):
            out.append(name)
        for key, value in node.items():
            if isinstance(key, str) and not _enabled(root, key):
                print(f"[prefetch]   skip {key} ({GATES[key]}=False)")
                continue
            _iter_models(value, out, root)
    elif isinstance(node, list):
        for item in node:
            _iter_models(item, out, root)


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: prefetch_models.py <pipeline_config.yaml> [...]", file=sys.stderr)
        return 2

    wanted: list[str] = []
    for path in sys.argv[1:]:
        cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        found: list[str] = []
        _iter_models(cfg, found, cfg)
        print(f"[prefetch] {path}: {len(found)} models -> {sorted(set(found))}")
        wanted.extend(found)

    # 重複排除しつつ順序維持
    unique = list(dict.fromkeys(wanted))
    from paddlex import create_model

    for i, name in enumerate(unique, 1):
        print(f"[prefetch] ({i}/{len(unique)}) {name}", flush=True)
        create_model(model_name=name)  # 未取得ならDLしてキャッシュへ

    print(f"[prefetch] done: {len(unique)} models cached")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
