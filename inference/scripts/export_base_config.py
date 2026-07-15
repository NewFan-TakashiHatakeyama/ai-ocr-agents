"""固定モデルの完全 base config を生成する（付録C-1 / C-2）。

paddleocr がインストールされた環境で実行し、当該バージョンの完全な PaddleX
パイプライン設定を出力する。出力を pipeline_config.yaml の値とマージして
authoritative config を作る。

    python inference/scripts/export_base_config.py structure -o /tmp/structure_base.yaml
    python inference/scripts/export_base_config.py ocr -o /tmp/ocr_base.yaml

paddleocr 未インストール環境では明示的にエラー終了する（本体スクリプトの副作用なし）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _build_structure():  # type: ignore[no-untyped-def]
    from paddleocr import PPStructureV3

    # 設計で固定する値をすべて渡す（出力がそのまま authoritative config になる）。
    # DD-03: OCR は PP-OCRv6_medium 固定。layout は PP-DocLayout_plus-L（PP-DocLayoutV3 は実在しない）。
    # DD-01/ADR-0002: 前処理は ingest 側で実施済みのため向き補正・歪み補正を無効化。
    # 印章 OCR: 認識も v6 に固定（既定は PP-OCRv5_server_rec で DD-03 違反になる）。
    # 検出は paddlex 3.7.2 に v6 版が存在せず PP-OCRv4_server_seal_det のみ（DD-03 の既知例外）。
    return PPStructureV3(
        text_detection_model_name="PP-OCRv6_medium_det",
        text_recognition_model_name="PP-OCRv6_medium_rec",
        layout_detection_model_name="PP-DocLayout_plus-L",
        seal_text_recognition_model_name="PP-OCRv6_medium_rec",
        use_seal_recognition=True,
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_formula_recognition=False,
        use_chart_recognition=False,
    )


def _build_ocr():  # type: ignore[no-untyped-def]
    from paddleocr import PaddleOCR

    return PaddleOCR(
        text_detection_model_name="PP-OCRv6_medium_det",
        text_recognition_model_name="PP-OCRv6_medium_rec",
        return_word_box=True,
    )


BUILDERS = {"structure": _build_structure, "ocr": _build_ocr}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pipeline", choices=sorted(BUILDERS))
    parser.add_argument("-o", "--output", type=Path, required=True)
    args = parser.parse_args()

    try:
        obj = BUILDERS[args.pipeline]()
    except ModuleNotFoundError:
        print(
            "paddleocr がインストールされていません。サービング用の環境で実行してください。",
            file=sys.stderr,
        )
        return 2

    obj.export_paddlex_config_to_yaml(str(args.output))
    print(f"[OK] wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
