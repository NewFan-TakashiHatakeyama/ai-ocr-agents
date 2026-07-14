"""実 PP-StructureV3 を Python 直実行して契約テスト fixture を録画する（付録C-3）。

deploy の HTTP サービング（paddlex --serve）は paddle 3.3.x + oneDNN(PIR) の未実装
（ConvertPirAttribute2RuntimeAttribute）で /layout-parsing が 500 になる。回避のため
paddleocr の PPStructureV3 を enable_mkldnn=False で直実行し、/layout-parsing 相当の
エンベロープ（prunedResult = predict の res）を fixtures に保存する。

依存は本体に入れない（重量のため）。実行時のみ:
    uv run --no-project --with paddlepaddle --with paddleocr --with "paddlex[ocr]" \
        python scripts/record_fixtures_local.py --image sample.png

出力: packages/paddle_client/tests/fixtures/real_layout_parsing_sample.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "packages/paddle_client/tests/fixtures/real_layout_parsing_sample.json"
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", type=Path, default=Path("sample.png"))
    parser.add_argument("--out", type=Path, default=_FIXTURE)
    args = parser.parse_args()

    from paddleocr import PPStructureV3  # 実行時のみ（runtime extra）

    pipe = PPStructureV3(
        enable_mkldnn=False,  # paddle 3.3.x oneDNN/PIR 回避（付録C-3）
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_formula_recognition=False,
        use_chart_recognition=False,
    )
    results = list(pipe.predict(str(args.image)))
    inner = results[0].json.get("res", results[0].json)
    # /layout-parsing サービング応答相当（prunedResult = predict の res）
    envelope = {"layoutParsingResults": [{"prunedResult": inner, "markdown": {"text": ""}}]}
    args.out.write_text(json.dumps(envelope, ensure_ascii=False, indent=2), encoding="utf-8")
    ocr = inner.get("overall_ocr_res", {})
    print(f"[OK] {args.out} に保存（rec_texts={len(ocr.get('rec_texts', []))} 件）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
