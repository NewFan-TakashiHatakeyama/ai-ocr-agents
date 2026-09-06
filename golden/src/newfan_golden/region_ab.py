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
import math
import sys
import time
import unicodedata
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

    # (doc_id, field, trial) をキーにした対応表。同じ帳票・同じ試行番号の
    # control と treat を**対にして**数えるために持つ。全体率の引き算だけでは
    # 「差がノイズの範囲か」を判断できない（前回の実測の弱点）。
    paired: dict[tuple[str, str, int], dict[str, bool]] = field(default_factory=dict)

    def note(
        self, doc_id: str, name: str, positional: bool, arm: str, hit: bool, trial: int = -1
    ) -> None:
        key = f"{doc_id}::{name}"
        t = self.per_field.setdefault(key, FieldTally(name=key, position_dependent=positional))
        t.position_dependent = t.position_dependent or positional
        if arm == "control":
            t.control_trials += 1
            t.control_hits += int(hit)
        else:
            t.treat_trials += 1
            t.treat_hits += int(hit)
        if trial >= 0:
            self.paired.setdefault((doc_id, name, trial), {})[arm] = hit


def mcnemar(
    paired: dict[tuple[str, str, int], dict[str, bool]],
    only: Optional[set[str]] = None,
) -> dict[str, Any]:
    """対応のある二値結果の McNemar 検定（正確二項）。

    同じ帳票・同じ試行番号で control と treat を対にし、**片方だけ当たった対**
    （不一致対）だけを数える。両方当たり／両方外れの対は差の情報を持たない。

    前回の実測は全体率 0.864 対 0.780 を目視で比べただけで、「この差がノイズか」
    を判断する根拠が無かった。不一致対が少なければ、率の差が大きく見えても
    何も言えない ── それを数字で出す。"""
    b = c = 0  # b: control だけ正解 / c: treat だけ正解
    for key, arms in paired.items():
        if only is not None and key[1] not in only:
            continue
        if "control" not in arms or "treat" not in arms:
            continue  # 片方の抽出が失敗した対は使えない
        if arms["control"] and not arms["treat"]:
            b += 1
        elif arms["treat"] and not arms["control"]:
            c += 1
    n = b + c
    if n == 0:
        return {"control_only": 0, "treat_only": 0, "discordant": 0, "p_value": None,
                "verdict": "不一致対が 0。差を論じる材料が無い"}
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / (2.0**n)
    p = min(1.0, 2.0 * tail)
    if p < 0.05:
        verdict = "介入が有意に良い" if c > b else "介入が有意に悪い"
    else:
        verdict = "有意差なし（この試行数では差を示せない）"
    return {
        "control_only": b,
        "treat_only": c,
        "discordant": n,
        "p_value": p,
        "verdict": verdict,
    }


def _norm(v: Optional[str]) -> str:
    """比較用の正規化。**表記の揺れは同一視する。**

    測りたいのは「正しい実体を選べたか」（発行元と宛先を取り違えていないか）で
    あって字面ではない。実測すると、抽出値と正解の差の大半が次の 3 つだった:

      - 敬称: 「大熊和一様」 vs 「大熊 和一」
      - 全角半角: 「１８丁目」 vs 「18丁目」
      - 区切り・通貨記号: 「395,217」 vs 「395217」

    これを別物として数えると、**両アームとも同じだけ外れて差が見えなくなる**
    （実際に 1/7 まで落ちた）。正規化は対照・介入に同じく効くので、比較の
    公平さは崩れない。逆に、値そのものの取り違えや欠落は正規化しても残る。
    """
    if v is None:
        return ""
    t = unicodedata.normalize("NFKC", str(v))
    t = "".join(t.split())
    for ch in (",", "￥", "¥", "円", "-", "−", "－", "ー", "‐", "･", "・"):
        t = t.replace(ch, "")
    # 宛名の敬称。紙面には付くが「誰宛か」の判定には関係しない
    for suffix in ("様", "御中", "殿", "行", "宛"):
        if t.endswith(suffix):
            t = t[: -len(suffix)]
    return t.casefold()


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
    # 位置でしか区別できない項目名の集合（doc_type をまたいで合算する）
    positional_names = {n for names in positional_map.values() for n in names}
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
                        tally.note(doc.document_id, name, name in positional, arm, hit, i)
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
        # **差がノイズの範囲かどうか**を数字で出す（率の引き算だけでは判断できない）
        "mcnemar_all": mcnemar(tally.paired),
        "mcnemar_positional": mcnemar(tally.paired, only=positional_names),
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
