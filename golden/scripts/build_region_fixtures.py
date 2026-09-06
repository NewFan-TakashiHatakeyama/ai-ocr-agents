"""samples/ の正解データから Phase 4 の A/B 用フィクスチャを組み立てる。

2 つのシナリオを別々に作る。**どちらも実運用に存在する経路**だが、領域の由来が
違うので混ぜてはいけない。

  S1「テンプレート化した帳票を再抽出する」
      その帳票自身の検出位置から領域を起こし、同じ帳票に当てる。
      検証画面の「この帳票を再抽出」がまさにこれ。領域は機械的に正確な側に寄る。

  S2「テンプレートを次の帳票に当てる」
      **同じ雛形の別の帳票**から起こした領域を当てる。テンプレート化の本来の目的で、
      ここで効かないなら機能としての価値は無い。同じ雛形の組は目視で確定した
      （samples_ground_truth.json の _layout_families）。

S1 は 21 枚（発行元と宛先の両方が紙面にある帳票すべて）、S2 は 5 枚しか作れない。
帳簿・決算書・集計表など発行元/宛先ブロックを持たない 8 枚は、位置の仮説を
検証できないので両方から外す。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
GT = ROOT / "golden" / "data" / "samples_ground_truth.json"
OUT = ROOT / "golden" / "data"

# 位置でしか区別できない項目。全体平均は「もともと簡単な項目」に薄められるので、
# ここを別建てで数える（region_ab.py の _positional）。
POSITIONAL = ["issuer_name", "issuer_address", "customer_name", "customer_address", "total_amount"]
FIELDS = POSITIONAL + ["document_date", "document_no"]
# critical 扱いにする項目（会計連携で誤ると実害が出る側）
CRITICAL = {"issuer_name", "customer_name", "total_amount"}


def _stem(filename: str) -> str:
    return filename.split(".")[0]


def build() -> dict[str, Any]:
    gt = json.loads(GT.read_text(encoding="utf-8"))
    docs = gt["documents"]
    families: dict[str, list[str]] = {
        k: v for k, v in gt["_layout_families"].items() if not k.startswith("_")
    }

    # 発行元と宛先の両方が紙面にある帳票だけを使う
    usable = [
        f for f, r in docs.items()
        if r["fields"].get("issuer_name") and r["fields"].get("customer_name")
    ]

    def gold_line(doc_type: str, filename: str) -> dict[str, Any]:
        rec = docs[filename]
        fields = [
            {"name": n, "value": rec["fields"][n], "critical": n in CRITICAL}
            for n in FIELDS
            if rec["fields"].get(n)
        ]
        return {
            "document_id": f"{doc_type}",
            "doc_type": doc_type,
            "image_uri": f"samples/{filename}",
            "fields": fields,
        }

    # ---- S1: 自分自身から起こした領域を当てる ----
    s1_gold = [gold_line(f"s1_{_stem(f)}", f) for f in usable]
    s1_spec = {
        f"s1_{_stem(f)}": {"template_image": f"samples/{f}", "fields": FIELDS} for f in usable
    }
    # 正解値の位置から領域を起こすための spec（人が矩形を引く操作の再現）。
    # 抽出結果の bbox から起こすと、AI が取り違えた項目では領域まで間違う。
    s1_goldspec = {
        f"s1_{_stem(f)}": {
            "template_image": f"samples/{f}",
            "gold": {n: docs[f]["fields"][n] for n in FIELDS if docs[f]["fields"].get(n)},
        }
        for f in usable
    }

    # ---- S2: 同じ雛形の別の帳票から起こした領域を当てる（leave-one-out）----
    # 組の全員を採点対象にできるよう、各帳票の領域は**別の 1 枚**から起こす。
    s2_gold: list[dict[str, Any]] = []
    s2_source: dict[str, str] = {}
    for members in families.values():
        if len(members) < 2:
            continue
        for i, target in enumerate(members):
            source = members[(i + 1) % len(members)]  # 自分以外の 1 枚
            doc_type = f"s2_{_stem(target)}"
            s2_gold.append(gold_line(doc_type, target))
            s2_source[doc_type] = source
    s2_spec = {
        dt: {"template_image": f"samples/{src}", "fields": FIELDS}
        for dt, src in s2_source.items()
    }
    # S2 は**テンプレート元の帳票の**正解値をその帳票の紙面上で探す
    # （採点対象の値ではない。テンプレートは元の帳票を見て引くものだから）。
    s2_goldspec = {
        dt: {
            "template_image": f"samples/{src}",
            "gold": {n: docs[src]["fields"][n] for n in FIELDS if docs[src]["fields"].get(n)},
        }
        for dt, src in s2_source.items()
    }

    return {
        "s1_gold": s1_gold,
        "s1_spec": s1_spec,
        "s1_goldspec": s1_goldspec,
        "s2_gold": s2_gold,
        "s2_spec": s2_spec,
        "s2_goldspec": s2_goldspec,
        "s2_source": s2_source,
        "usable": usable,
        "excluded": sorted(set(docs) - set(usable)),
    }


def main() -> int:
    b = build()
    positional_s1 = {d["doc_type"]: POSITIONAL for d in b["s1_gold"]}
    positional_s2 = {d["doc_type"]: POSITIONAL for d in b["s2_gold"]}

    def write_jsonl(path: Path, header: list[str], rows: list[dict[str, Any]]) -> None:
        lines = [f"// {h}" for h in header]
        lines += [json.dumps(r, ensure_ascii=False) for r in rows]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"wrote {path} ({len(rows)} 件)")

    write_jsonl(
        OUT / "region_ab_s1.jsonl",
        [
            "S1「テンプレート化した帳票を再抽出する」の A/B 用フィクスチャ。",
            "領域はその帳票自身の検出位置から起こす（検証画面の再抽出導線と同じ）。",
            "正解値は golden/data/samples_ground_truth.json（二重ラベリング＋裁定）由来。",
            "image_uri はリポジトリ相対。画像は samples/ に置くこと。",
        ],
        b["s1_gold"],
    )
    write_jsonl(
        OUT / "region_ab_s2.jsonl",
        [
            "S2「テンプレートを次の帳票に当てる」の A/B 用フィクスチャ。",
            "領域は**同じ雛形の別の帳票**から起こす（leave-one-out）。テンプレート化の本来の目的。",
            "同じ雛形の組は目視で確定済み（samples_ground_truth.json の _layout_families）。",
        ],
        b["s2_gold"],
    )

    (OUT / "region_ab_s1_spec.json").write_text(
        json.dumps(b["s1_spec"], ensure_ascii=False, indent=1), encoding="utf-8"
    )
    (OUT / "region_ab_s2_spec.json").write_text(
        json.dumps(b["s2_spec"], ensure_ascii=False, indent=1), encoding="utf-8"
    )
    (OUT / "region_ab_s1_goldspec.json").write_text(
        json.dumps(b["s1_goldspec"], ensure_ascii=False, indent=1), encoding="utf-8"
    )
    (OUT / "region_ab_s2_goldspec.json").write_text(
        json.dumps(b["s2_goldspec"], ensure_ascii=False, indent=1), encoding="utf-8"
    )
    (OUT / "region_ab_s1_positional.json").write_text(
        json.dumps({"_positional": positional_s1}, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    (OUT / "region_ab_s2_positional.json").write_text(
        json.dumps({"_positional": positional_s2}, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    print(f"S1 対象 {len(b['s1_gold'])} 枚 / S2 対象 {len(b['s2_gold'])} 枚")
    print("S2 の領域の出どころ:", json.dumps(b["s2_source"], ensure_ascii=False))
    print(f"除外 {len(b['excluded'])} 枚（発行元/宛先ブロックが無い）:", ", ".join(b["excluded"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
