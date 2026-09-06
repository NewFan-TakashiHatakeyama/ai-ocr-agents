"""位置ガードの shadow 実測（Phase 5 のゲート）。

設計 D10 / §9 Phase 5 のゲートは「shadow 実測に基づき許容パラメータを確定し、
**誤 mismatch 率が許容水準**であることを確認してから ``REGION_GUARD_ENFORCE`` を on」。
その実測を回すのがこのモジュール。

測り方の要点:

- **正しい領域を与えた run で mismatch が出たら、それは誤検知である。** 領域は
  その帳票で実際に検出された位置から作るので、同じ帳票を同じ領域で抽出して
  「設定領域外で検出された」と言われたら、ガードが厳しすぎるということになる。
- したがって知りたいのは「有効化したとき、正常な帳票のうち何割が余計にレビューへ
  回るか」。これが高いとレビュー工数が増えるだけで価値が無い。
- 併せて **ガードが効くべきケース**（意図的にずらした領域）も測る。誤検知が 0 でも
  検知力が 0 なら有効化する意味が無いため、両方を見て初めて判断できる。

出力は JSON。判定は人が読んで決める（このスクリプトは有効化可否を自動で決めない）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

import httpx

from newfan_golden.dataset import load_jsonl
from newfan_golden.region_ab import (
    RegionAbError,
    _extract,
    _put_schema,
    _result,
    _upload,
)


def _shift(rect: list[float], dy: float) -> list[float]:
    """領域を縦にずらす（ガードが検知すべきケースを作る）。"""
    y1 = max(0.0, min(1.0, rect[1] + dy))
    y2 = max(0.0, min(1.0, rect[3] + dy))
    if y1 >= y2:
        y1, y2 = min(y1, y2), max(y1, y2) + 0.02
    return [rect[0], y1, rect[2], min(1.0, y2)]


def run(
    gold: Path,
    regions_path: Path,
    api: str,
    token: str,
    trials: int,
    shift: float,
    timeout_sec: float,
) -> dict[str, Any]:
    docs = load_jsonl(gold)
    regions = json.loads(regions_path.read_text(encoding="utf-8"))
    headers = {"Authorization": f"Bearer {token}"}
    rows: list[dict[str, Any]] = []

    with httpx.Client(base_url=api.rstrip("/"), headers=headers, timeout=60.0) as client:
        for doc in docs:
            if not doc.image_uri or not doc.doc_type:
                raise RegionAbError(f"{doc.document_id}: image_uri と doc_type が要ります")
            doc_regions = regions.get(doc.doc_type, {})
            if not doc_regions:
                continue
            base = [
                {"name": f.name, "label": f.name, "type": "string",
                 "required": False, "critical": bool(f.critical)}
                for f in doc.fields
            ]
            first = next(iter(doc_regions))
            arms = {
                # 正しい領域: ここで出る mismatch は**すべて誤検知**
                "aligned": {n: r for n, r in doc_regions.items()},
                # 全項目をずらす = 「別レイアウトの帳票が来た」ケース。
                # ガードは doc レベルで「別レイアウト」と判定し per-field レビューを
                # **抑止する**（取引先 B の帳票を全件レビュー化させないため）。
                "shifted_all": {n: {**r, "rect": _shift(list(r["rect"]), shift)}
                                 for n, r in doc_regions.items()},
                # **1 項目だけ**ずらす = 有効化して初めて挙動が変わる経路。
                # 少数だけの不一致なので doc レベル抑止に掛からず、その項目に
                # per-field のレビュー所見が付く。ここを測らないと「有効化して何が
                # 増えるか」が分からない。
                "shifted_one": {
                    n: ({**r, "rect": _shift(list(r["rect"]), shift)} if n == first else r)
                    for n, r in doc_regions.items()
                },
            }
            schemas = {}
            for arm, rmap in arms.items():
                schemas[arm] = _put_schema(
                    client,
                    {
                        "doc_type": f"guard_{arm}_{doc.doc_type}",
                        "fields": [
                            {**f, **({"region": rmap[f["name"]]} if f["name"] in rmap else {})}
                            for f in base
                        ],
                        "source_page_count": 1,
                        "create": False,
                    },
                )

            document_id = _upload(client, Path(doc.image_uri))
            print(f"[guard] {doc.document_id}: doc={document_id}", flush=True)
            for i in range(trials):
                for arm, schema in schemas.items():
                    status = _extract(client, document_id, str(schema["id"]), timeout_sec)
                    if status != "succeeded":
                        print(f"  trial {i} {arm}: 抽出 {status}（捨てる）", flush=True)
                        continue
                    res = _result(client, document_id)
                    stats = res.get("region_stats") or {}
                    mismatched = list(stats.get("mismatch_fields", []) or [])
                    graded = [
                        f["name"] for f in res.get("fields", [])
                        if f.get("bbox") and f["name"] in arms[arm]
                    ]
                    rows.append({
                        "document_id": doc.document_id,
                        "arm": arm,
                        "trial": i,
                        "regions": len(arms[arm]),
                        "graded": len(graded),
                        "mismatched": len(mismatched),
                        "mismatch_fields": mismatched,
                        "layout_mismatch": bool(stats.get("layout_mismatch")),
                    })
                    print(f"  trial {i} {arm}: 判定対象 {len(graded)} / mismatch "
                          f"{len(mismatched)} {mismatched}", flush=True)
            client.delete(f"/documents/{document_id}")

    def _agg(arm: str) -> dict[str, Any]:
        sel = [r for r in rows if r["arm"] == arm]
        graded = sum(r["graded"] for r in sel)
        mis = sum(r["mismatched"] for r in sel)
        return {
            "runs": len(sel),
            "graded_fields": graded,
            "mismatched_fields": mis,
            "rate": (mis / graded) if graded else None,
            "runs_with_any_mismatch": sum(1 for r in sel if r["mismatched"]),
            "runs_with_layout_mismatch": sum(1 for r in sel if r["layout_mismatch"]),
        }

    return {
        "trials": trials,
        "shift": shift,
        # 正しい領域で出た mismatch = 誤検知
        "false_positive": _agg("aligned"),
        # 全項目ずれ = 別レイアウト判定（per-field レビューは抑止される）
        "detection_layout": _agg("shifted_all"),
        # 1 項目だけずれ = 有効化で per-field レビューが増える経路
        "detection_single": _agg("shifted_one"),
        "rows": rows,
    }


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="位置ガードの shadow 実測（Phase 5 のゲート）")
    ap.add_argument("--gold", required=True, type=Path)
    ap.add_argument("--regions", required=True, type=Path)
    ap.add_argument("--api", required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--shift", type=float, default=0.12, help="検知側で領域を縦にずらす割合")
    ap.add_argument("--timeout-sec", type=float, default=600.0)
    args = ap.parse_args(argv)

    report = run(args.gold, args.regions, args.api, args.token,
                 args.trials, args.shift, args.timeout_sec)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: report[k] for k in
                      ("false_positive", "detection_layout", "detection_single")},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
