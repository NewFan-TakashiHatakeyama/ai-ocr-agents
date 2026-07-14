"""実サービングの応答を paddle_client の契約テスト fixture として録画する（付録C-1/C-3）。

deploy/compose.yaml で structure/ocr サービングを起動し、代表帳票画像を投げて応答 JSON を
packages/paddle_client/tests/fixtures/ に保存する。特に return_word_box=True 時の単語/単文字
座標の実フィールド名を確認し、schema.OverallOcrRes の候補名を実名に確定する。

使い方:
    uv run python scripts/record_fixtures.py --image sample.png \
        --structure-url http://localhost:8081 --ocr-url http://localhost:8082
"""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path

import httpx

_FIXTURE_DIR = Path(__file__).resolve().parents[1] / "packages/paddle_client/tests/fixtures"


def _encode(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def _post(url: str, endpoint: str, payload: dict[str, object]) -> dict[str, object]:
    resp = httpx.post(f"{url.rstrip('/')}{endpoint}", json=payload, timeout=120)
    resp.raise_for_status()
    return resp.json()  # type: ignore[no-any-return]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--structure-url", default="http://localhost:8081")
    parser.add_argument("--ocr-url", default="http://localhost:8082")
    args = parser.parse_args()

    b64 = _encode(args.image)

    layout = _post(
        args.structure_url,
        "/layout-parsing",
        {
            "file": b64,
            "fileType": 1,
            "useDocOrientationClassify": False,
            "useDocUnwarping": False,
            "useFormulaRecognition": False,
            "useChartRecognition": False,
            "visualize": False,
        },
    )
    ocr = _post(args.ocr_url, "/ocr", {"file": b64, "fileType": 1, "visualize": False})

    (_FIXTURE_DIR / "layout_parsing_response.json").write_text(
        json.dumps(layout, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (_FIXTURE_DIR / "ocr_response.json").write_text(
        json.dumps(ocr, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[OK] fixtures を {_FIXTURE_DIR} に保存しました")
    print("次: schema.OverallOcrRes の rec_word_boxes 等の実フィールド名を ocr_response.json で確認")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
