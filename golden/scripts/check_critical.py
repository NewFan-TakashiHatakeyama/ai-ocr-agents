"""ゴールデンセットの critical と field_schemas の critical が一致するか検査する（§14.2）。

critical_exact_match は「critical 項目だけ見た正解率」で、リリース判断の中心にある。
schemas.json 側だけ critical を足す/外すと、ゲートの数字が実態からずれたまま通る。
機械で突き合わせないと気づけないので CI で回す。

  uv run python golden/scripts/check_critical.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from newfan_golden.dataset import load_jsonl

ROOT = Path(__file__).resolve().parents[2]
GOLD = ROOT / "golden/data/dev.jsonl"
SCHEMAS = ROOT / "golden/data/schemas.json"


def main() -> int:
    schemas = json.loads(SCHEMAS.read_text(encoding="utf-8"))["schemas"]
    by_id = {s["id"]: s for s in schemas}
    problems: list[str] = []

    for doc in load_jsonl(GOLD):
        schema = by_id.get(doc.schema_id or "")
        if schema is None:
            problems.append(f"{doc.document_id}: schema_id={doc.schema_id!r} が schemas.json に無い")
            continue

        want = {f["name"]: bool(f.get("critical", False)) for f in schema["fields"]}
        for g in doc.fields:
            if g.name not in want:
                # スキーマに無い項目は KIE が抽出しない＝必ず Recall 0 になる。
                problems.append(
                    f"{doc.document_id}: 項目 {g.name!r} が {schema['id']} に定義されていない"
                )
            elif want[g.name] != g.critical:
                problems.append(
                    f"{doc.document_id}: {g.name!r} の critical が不一致"
                    f"（gold={g.critical} / {schema['id']}={want[g.name]}）"
                )

    if problems:
        print("[golden] critical の整合が取れていません:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print("[golden] critical はスキーマと一致しています")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
