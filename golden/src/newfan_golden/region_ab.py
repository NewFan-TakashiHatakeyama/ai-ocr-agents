"""読取領域ヒント（Phase 4）と位置ガード（Phase 5）の実測。

設計 docs/design/region-template-editor.md §5.6 / §9 のゲートは
「**fixture ベースの精度計測で改善が確認できた場合のみ出荷**」である。
この実測を回すのがこのモジュール。

  # ヒント無し（対照）と有り（介入）を同じ帳票で交互に N 回ずつ回して比較する
  uv run python -m newfan_golden.region_ab \\
      --gold golden/data/region_ab.jsonl --api http://localhost:8000/v1 \\
      --token "$JWT" --trials 5 --out out/region_ab.json

設計の要点:

- **同じ帳票・同じスキーマ定義**で、``region`` の有無だけを変えた 2 版を作って比べる。
  他を揃えないと、差が領域のせいなのか別の要因なのか分からない。
- LLM は非決定的なので **1 回の比較では何も言えない**。同じ条件を N 回繰り返し、
  「対照と介入を交互に」実行する（時間帯によるモデル側の揺れを両条件へ均等に散らす）。
- 見るのは全体の正解率だけではない。**位置でしか区別できない項目**（発行元 vs 宛先、
  複数箇所に出る合計金額）を別建てで数える。全体平均は「もともと簡単な項目」に
  薄められて効果が見えなくなる。
- **悪化の検出**も同じ重みで見る。ヒントが領域外の正しい値を捨てさせていないか
  （設計が最も恐れる失敗）を、項目ごとの勝敗で数える。

出力は JSON。判定は人が読んで決める（このスクリプトは出荷可否を自動で決めない）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx

from newfan_golden.dataset import GoldenDoc, load_jsonl


class RegionAbError(RuntimeError):
    pass


@dataclass
class FieldTally:
    """項目 1 つの勝敗。対照/介入それぞれで何回正解したか。"""

    name: str
    position_dependent: bool = False
    control_hits: int = 0
    treat_hits: int = 0
    # **アームごとに分母を持つ**。抽出が失敗した試行はそのアームだけ捨てるので、
    # 共通の trials で数えると「5/4」のような読めない比が出る（実際に出した）。
    control_trials: int = 0
    treat_trials: int = 0

    def as_dict(self) -> dict[str, Any]:
        c = (self.control_hits / self.control_trials) if self.control_trials else None
        t = (self.treat_hits / self.treat_trials) if self.treat_trials else None
        return {
            "name": self.name,
            "position_dependent": self.position_dependent,
            "control": f"{self.control_hits}/{self.control_trials}",
            "treat": f"{self.treat_hits}/{self.treat_trials}",
            "control_rate": c,
            "treat_rate": t,
            "delta_rate": (t - c) if (c is not None and t is not None) else None,
        }


@dataclass
class Tally:
    per_field: dict[str, FieldTally] = field(default_factory=dict)
    control_runs: list[dict[str, Any]] = field(default_factory=list)
    treat_runs: list[dict[str, Any]] = field(default_factory=list)

    def note(self, doc_id: str, name: str, positional: bool, arm: str, hit: bool) -> None:
        key = f"{doc_id}::{name}"
        t = self.per_field.setdefault(key, FieldTally(name=key, position_dependent=positional))
        t.position_dependent = t.position_dependent or positional
        if arm == "control":
            t.control_trials += 1
            t.control_hits += int(hit)
        else:
            t.treat_trials += 1
            t.treat_hits += int(hit)


def _norm(v: Optional[str]) -> str:
    """比較用の正規化。ゴールデンの value は正規化済み表現なので軽く揃えるだけ。"""
    if v is None:
        return ""
    return "".join(str(v).split()).replace(",", "").replace("￥", "").replace("¥", "")


def _wait_job(client: httpx.Client, job_id: str, timeout_sec: float) -> str:
    deadline = time.time() + timeout_sec
    status = "?"
    while time.time() < deadline:
        r = client.get(f"/jobs/{job_id}")
        r.raise_for_status()
        status = str(r.json().get("status", "?"))
        if status in ("succeeded", "failed", "dead"):
            return status
        time.sleep(3.0)
    return status


def _upload(client: httpx.Client, path: Path) -> str:
    with path.open("rb") as fh:
        r = client.post("/documents", files={"file": (path.name, fh, "application/octet-stream")})
    if r.status_code != 201:
        raise RegionAbError(f"アップロードに失敗しました: {r.status_code} {r.text[:200]}")
    return str(r.json()["document_id"])


def _extract(client: httpx.Client, document_id: str, schema_id: str, timeout_sec: float) -> str:
    r = client.post(
        f"/documents/{document_id}/extract",
        json={"schema_id": schema_id, "options": {}, "supersede_review": True},
    )
    if r.status_code != 202:
        raise RegionAbError(f"抽出を開始できません: {r.status_code} {r.text[:200]}")
    return _wait_job(client, str(r.json()["job_id"]), timeout_sec)


def _result(client: httpx.Client, document_id: str) -> dict[str, Any]:
    r = client.get(f"/documents/{document_id}/result")
    r.raise_for_status()
    return dict(r.json())


def _put_schema(client: httpx.Client, body: dict[str, Any]) -> dict[str, Any]:
    r = client.put("/schemas", json=body)
    if r.status_code != 200:
        raise RegionAbError(f"スキーマ保存に失敗しました: {r.status_code} {r.text[:300]}")
    return dict(r.json())


def run(
    docs: list[GoldenDoc],
    regions: dict[str, Any],
    api: str,
    token: str,
    trials: int,
    timeout_sec: float,
) -> dict[str, Any]:
    """対照（領域なし）と介入（領域あり）を交互に trials 回ずつ回す。

    regions は {doc_type: {field_name: {"page":..,"rect":[..]}}} と
    {"_positional": {doc_type: [field_name, ...]}} を持つ。
    """
    positional_map = regions.get("_positional", {})
    tally = Tally()
    headers = {"Authorization": f"Bearer {token}"}

    with httpx.Client(base_url=api.rstrip("/"), headers=headers, timeout=60.0) as client:
        for doc in docs:
            if not doc.image_uri or not doc.doc_type:
                raise RegionAbError(
                    f"{doc.document_id}: image_uri と doc_type の両方が要ります"
                )
            image = Path(doc.image_uri)
            if not image.exists():
                raise RegionAbError(f"画像が見つかりません: {image}")
            gold = {f.name: _norm(f.value) for f in doc.fields}
            positional = set(positional_map.get(doc.doc_type, []))
            base_fields = [
                {
                    "name": f.name,
                    "label": f.name,
                    "type": "string",
                    "required": False,
                    "critical": bool(f.critical),
                }
                for f in doc.fields
            ]
            doc_regions = regions.get(doc.doc_type, {})

            # 同じ項目定義で region の有無だけが違う 2 版を作る
            control = _put_schema(
                client,
                {
                    "doc_type": f"ab_control_{doc.doc_type}",
                    "fields": base_fields,
                    "create": False,
                },
            )
            treat_fields = [
                {**f, **({"region": doc_regions[f["name"]]} if f["name"] in doc_regions else {})}
                for f in base_fields
            ]
            treat = _put_schema(
                client,
                {
                    "doc_type": f"ab_treat_{doc.doc_type}",
                    "fields": treat_fields,
                    "create": False,
                },
            )

            document_id = _upload(client, image)
            print(f"[region_ab] {doc.document_id}: doc={document_id}", flush=True)

            for i in range(trials):
                # 交互に回す（時間帯によるモデル側の揺れを両条件へ均等に散らす）
                for arm, schema in (("control", control), ("treat", treat)):
                    status = _extract(client, document_id, str(schema["id"]), timeout_sec)
                    if status != "succeeded":
                        print(f"  trial {i} {arm}: 抽出 {status}（この試行は捨てる）", flush=True)
                        continue
                    res = _result(client, document_id)
                    got = {
                        f["name"]: _norm(f.get("value_normalized") or f.get("value_raw"))
                        for f in res.get("fields", [])
                    }
                    hits = 0
                    for name, want in gold.items():
                        hit = bool(want) and got.get(name, "") == want
                        hits += int(hit)
                        tally.note(doc.document_id, name, name in positional, arm, hit)
                    row = {
                        "document_id": doc.document_id,
                        "trial": i,
                        "hits": hits,
                        "total": sum(1 for v in gold.values() if v),
                        "run_id": res.get("run_id"),
                    }
                    (tally.control_runs if arm == "control" else tally.treat_runs).append(row)
                    print(f"  trial {i} {arm}: {hits}/{row['total']}", flush=True)

            client.delete(f"/documents/{document_id}")

    fields = [t.as_dict() for t in tally.per_field.values()]
    pos = [f for f in fields if f["position_dependent"]]

    def _rate(rows: list[dict[str, Any]]) -> Optional[float]:
        tot = sum(r["total"] for r in rows)
        return (sum(r["hits"] for r in rows) / tot) if tot else None

    return {
        "trials": trials,
        "control_exact_match": _rate(tally.control_runs),
        "treat_exact_match": _rate(tally.treat_runs),
        "control_runs": tally.control_runs,
        "treat_runs": tally.treat_runs,
        "fields": fields,
        "positional_fields": pos,
        "positional_delta_rate": sum((f["delta_rate"] or 0) for f in pos),
        "regressed_fields": [f for f in fields if (f["delta_rate"] or 0) < 0],
        "improved_fields": [f for f in fields if (f["delta_rate"] or 0) > 0],
    }


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="読取領域ヒントの A/B 実測（Phase 4 のゲート）")
    ap.add_argument("--gold", required=True, type=Path)
    ap.add_argument("--regions", required=True, type=Path, help="doc_type ごとの領域定義 JSON")
    ap.add_argument("--api", required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--trials", type=int, default=5)
    ap.add_argument("--timeout-sec", type=float, default=600.0)
    args = ap.parse_args(argv)

    docs = load_jsonl(args.gold)
    regions = json.loads(args.regions.read_text(encoding="utf-8"))
    report = run(docs, regions, args.api, args.token, args.trials, args.timeout_sec)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: report[k] for k in
                      ("control_exact_match", "treat_exact_match", "positional_delta_rate")},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
