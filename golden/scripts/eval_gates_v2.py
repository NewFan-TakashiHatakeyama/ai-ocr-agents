"""読取領域ヒントの計測（設計 region-field-add-and-hint-v2 §3）の出荷ゲート G1〜G4 を判定する。

region_ab の出力（S1 / S2 / S3 の JSON）を読んで、ゲートごとに **通った／落ちた** と
その根拠の数字を出す。G5（領域なしプロンプトのスナップショット一致）は
services/orchestrator/tests の単体テスト、G6（プロンプト長）は別計測なので、ここでは
扱わない。実測記録に貼るための Markdown も同時に出す。

G4 は第 4 回から「落とした／捨てた対で McNemar が『介入が有意に悪い』にならない」
（第 3 回までは「正解数の差が 0 以上」。効果が無くても半分の確率で落ちる欠陥があった）。

使い方:
    uv run python golden/scripts/eval_gates_v2.py --s1 out/s1_ab.json --s2 out/s2_ab.json \
        --s3 out/s3_ab.json [--md out/gates.md]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from newfan_golden.region_ab import mcnemar

# G2: 帳票ごとの純減（介入 − 対照、5 試行合計）の下限
G2_MIN_DOC_NET = -2
# G3: sample8 の customer_name で「捨てた／落とした」が 5 試行中この回数以上
G3_MIN_DEFENDED = 4
G3_DOC = "s2_sample8"
G3_FIELD = "customer_name"


def _load(p: Path | None) -> dict[str, Any] | None:
    if p is None or not p.exists():
        return None
    return dict(json.loads(p.read_text(encoding="utf-8")))


def _f(x: Any) -> str:
    """率の表示。片方のアームが全滅すると region_ab は None を書くので落ちないように。"""
    return f"{x:.3f}" if isinstance(x, (int, float)) else "—"


def _paired_field(report: dict[str, Any], doc: str, field: str) -> dict[str, int]:
    """1 項目を**同じ試行の対照と対にして**数える（率の引き算ではなく）。

    region_ab は失敗した試行をアームごとに捨てるので、"4/4" 対 "4/5" のように分母が
    ずれ得る。生の正解数を比べると 4 ≥ 4 で「悪化なし」に見えてしまう（レビュー指摘）。
    両アームが成功した試行だけを対にし、不一致対を数える。"""
    ctrl = {
        r["trial"]: (r.get("field_hits") or {}).get(field)
        for r in report.get("control_runs", [])
        if r["document_id"] == doc
    }
    treat = {
        r["trial"]: (r.get("field_hits") or {}).get(field)
        for r in report.get("treat_runs", [])
        if r["document_id"] == doc
    }
    out = {"pairs": 0, "control_only": 0, "treat_only": 0, "both": 0, "neither": 0}
    for t, c in ctrl.items():
        if c is None or t not in treat or treat[t] is None:
            continue
        out["pairs"] += 1
        if c and not treat[t]:
            out["control_only"] += 1
        elif treat[t] and not c:
            out["treat_only"] += 1
        elif c:
            out["both"] += 1
        else:
            out["neither"] += 1
    return out


def _docs_in(report: dict[str, Any]) -> list[str]:
    seen: list[str] = []
    for r in list(report.get("control_runs", [])) + list(report.get("treat_runs", [])):
        if r["document_id"] not in seen:
            seen.append(str(r["document_id"]))
    return seen


def _field_totals(report: dict[str, Any]) -> dict[str, dict[str, int]]:
    """帳票をまたいで項目名ごとに正解数／試行数を足す（前回の実測記録と同じ表）。"""
    tot: dict[str, dict[str, int]] = defaultdict(lambda: {"ch": 0, "ct": 0, "th": 0, "tt": 0})
    for f in report.get("fields", []):
        name = str(f["name"]).split("::", 1)[-1]
        ch, ct = (int(x) for x in str(f["control"]).split("/"))
        th, tt = (int(x) for x in str(f["treat"]).split("/"))
        e = tot[name]
        e["ch"] += ch
        e["ct"] += ct
        e["th"] += th
        e["tt"] += tt
    return dict(tot)


def _outcome_totals(report: dict[str, Any]) -> dict[str, int]:
    """介入アームの全 run で、ヒントがどう扱われたかの合計。"""
    tot: dict[str, int] = defaultdict(int)
    for r in report.get("treat_runs", []):
        h = r.get("hints") or {}
        tot["given"] += len(h.get("given") or [])
        for o in (h.get("outcomes") or {}).values():
            tot[str(o)] += 1
        for reason in (h.get("dropped") or {}).values():
            tot[f"dropped:{reason}"] += 1
    return dict(tot)


def gate_g1(s1: dict[str, Any] | None) -> tuple[bool | None, str]:
    if s1 is None:
        return None, "S1 の結果が無い"
    m = s1.get("mcnemar_all") or {}
    ok = m.get("verdict") == "介入が有意に良い"
    p = m.get("p_value")
    return ok, (
        f"S1 McNemar（全項目）: 対照のみ {m.get('control_only')} / 介入のみ {m.get('treat_only')}"
        f" / p = {p:.3f} → {m.get('verdict')}"
        if p is not None
        else f"S1: {m.get('verdict')}"
    )


def gate_g2(s3: dict[str, Any] | None) -> tuple[bool | None, str]:
    if s3 is None:
        return None, "S3 の結果が無い"
    m = s3.get("mcnemar_all") or {}
    not_worse = m.get("verdict") != "介入が有意に悪い"
    per_doc = s3.get("per_doc_net") or {}
    trials = int(s3.get("trials") or 0)
    nets = {d: int(v["net"]) for d, v in per_doc.items()}
    below = {d: n for d, n in nets.items() if n < G2_MIN_DOC_NET}
    # 「5 試行合計」が前提。片方のアームが失敗した試行は対に入らないので、対が
    # 試行数に満たない帳票（全滅して per_doc_net から消えた帳票を含む）は判定できない
    short = {
        d: int(per_doc.get(d, {}).get("pairs", 0))
        for d in _docs_in(s3)
        if int(per_doc.get(d, {}).get("pairs", 0)) < trials
    }
    p = m.get("p_value")
    ptxt = f"p = {p:.3f}" if p is not None else "p = —"
    why = (
        f"S3 McNemar: 対照のみ {m.get('control_only')} / 介入のみ {m.get('treat_only')} / {ptxt}"
        f" → {m.get('verdict')}；帳票ごとの純増減（{trials} 試行の対） {nets}"
    )
    if below:
        why += f"；{G2_MIN_DOC_NET} を下回る帳票 {below}"
    if short:
        why += f"；対が {trials} に満たない帳票（判定不能） {short}"
        return None, why
    return (not_worse and not below), why


def gate_g3(s2: dict[str, Any] | None) -> tuple[bool | None, str]:
    if s2 is None:
        return None, "S2 の結果が無い"
    key = f"{G3_DOC}::{G3_FIELD}"
    ft = next((f for f in s2.get("fields", []) if f["name"] == key), None)
    if ft is None:
        return None, f"{key} が S2 に無い"
    trials = int(s2.get("trials") or 0)
    pr = _paired_field(s2, G3_DOC, G3_FIELD)
    pf = ((s2.get("hint_summary") or {}).get("per_field") or {}).get(key) or {}
    dropped = dict(pf.get("dropped") or {})
    # 「守った」= 種類が合わずに落とした（kind_conflict）か、渡したが従わなかった（rejected）。
    # 他の理由（文字が無い等）で届かなかったのは「1 行ずれ」の再現ではないので別に示す
    defended = int(pf.get("rejected", 0)) + int(dropped.get("kind_conflict", 0))
    undelivered = sum(int(v) for r, v in dropped.items() if r != "kind_conflict")
    why = (
        f"sample8 customer_name: 対照 {ft['control']} / 介入 {ft['treat']}"
        f"（対 {pr['pairs']}: 対照のみ正解 {pr['control_only']} / 介入のみ正解 {pr['treat_only']}）；"
        f"kind_conflict で落とした {dropped.get('kind_conflict', 0)}"
        f" / 捨てた rejected {pf.get('rejected', 0)}"
        f" / followed {pf.get('followed', 0)} / partial {pf.get('partial', 0)}"
        f" / no_evidence {pf.get('no_evidence', 0)}"
        f"（守った回数 {defended}/{trials}、届かなかった {undelivered}: {dropped}）"
    )
    if pr["pairs"] < trials:
        return None, why + f"；対が {trials} に満たない（判定不能）"
    # 「対照以上」は対で見る: 対照だけ正解した試行が、介入だけ正解した試行より多くなければよい
    ok = pr["treat_only"] >= pr["control_only"] or defended >= G3_MIN_DEFENDED
    return ok, why


def _affected_paired(
    rows: list[dict[str, Any]],
) -> dict[tuple[str, str, int], dict[str, bool]]:
    """``hint_summary.affected_rows`` を mcnemar の対応表（(帳票, 項目, 試行) → 両アームの
    正誤）に直す。行は既に「同じ試行の対照と対にした」ものなので、両アームが必ず揃う。"""
    return {
        (str(r["document_id"]), str(r["field"]), int(r["trial"])): {
            "control": bool(r["control"]),
            "treat": bool(r["treat"]),
        }
        for r in rows
    }


def gate_g4(s3: dict[str, Any] | None) -> tuple[bool | None, str]:
    """落とした／捨てたヒントの項目で、介入が対照より**有意に**悪くなっていないか。

    第 3 回までは「正解数の差が 0 以上」で判定していた。効果が無ければ差は 0 を中心に
    散らばるので、効果が無くても半分の確率で落ちる（設計 §3 の注記）。第 4 回から、
    同じ対で McNemar（正確二項）を取り、「介入が有意に悪い」でなければ通す。生の差は
    根拠に残す（設計が恐れる「落としたことで悪くなる」の大きさが読めるように）。"""
    if s3 is None:
        return None, "S3 の結果が無い"
    rows = list(((s3.get("hint_summary") or {}).get("affected_rows")) or [])
    if not rows:
        return None, "S3 で落とした／捨てたヒントが 1 つも無い（判定材料なし）"
    control_hits = sum(int(bool(r["control"])) for r in rows)
    treat_hits = sum(int(bool(r["treat"])) for r in rows)
    m = mcnemar(_affected_paired(rows))
    p = m.get("p_value")
    ptxt = f"p = {p:.3f}" if p is not None else "p = —"
    why = (
        f"S3 で落とした／捨てた項目 {len(rows)} 対: 対照 {control_hits} 正解 /"
        f" 介入 {treat_hits} 正解（差 {treat_hits - control_hits:+d}）；"
        f"McNemar: 対照のみ {m['control_only']} / 介入のみ {m['treat_only']} / {ptxt}"
        f" → {m['verdict']}"
    )
    return m["verdict"] != "介入が有意に悪い", why


def _md_field_table(reports: dict[str, dict[str, Any] | None]) -> str:
    names: list[str] = []
    per_arm: dict[str, dict[str, dict[str, int]]] = {}
    for arm, rep in reports.items():
        if rep is None:
            continue
        per_arm[arm] = _field_totals(rep)
        for n in per_arm[arm]:
            if n not in names:
                names.append(n)
    arms = list(per_arm)
    head = "| 項目 | " + " | ".join(f"{a} 対照 | {a} 介入" for a in arms) + " |"
    sep = "|---|" + "---|---|" * len(arms)
    lines = [head, sep]
    for n in names:
        cells = []
        for a in arms:
            e = per_arm[a].get(n)
            if e is None:
                cells += ["—", "—"]
                continue
            c = f"{e['ch']}/{e['ct']}"
            t = f"{e['th']}/{e['tt']}"
            # 分母がアームごとにずれ得るので率で比べる（生の正解数だと 4/4 対 4/5 が同点に見える）
            cr = e["ch"] / e["ct"] if e["ct"] else None
            tr = e["th"] / e["tt"] if e["tt"] else None
            if cr is not None and tr is not None:
                if tr > cr:
                    t = f"**{t}**"
                elif tr < cr:
                    t = f"{t} ▼"
            cells += [c, t]
        lines.append(f"| {n} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _md_doc_table(rep: dict[str, Any]) -> str:
    lines = [
        "| 帳票 | 対 | 対照 | 介入 | 純増減 | ヒント given / dropped / followed / partial / rejected / no_evidence |",
        "|---|---|---|---|---|---|",
    ]
    per_doc = rep.get("per_doc_net") or {}
    trials = int(rep.get("trials") or 0)
    pf = (rep.get("hint_summary") or {}).get("per_field") or {}
    for doc in _docs_in(rep):
        v = per_doc.get(doc) or {"pairs": 0, "control_hits": 0, "treat_hits": 0, "net": 0}
        tot: dict[str, int] = defaultdict(int)
        dropped = 0
        for key, e in pf.items():
            if not key.startswith(f"{doc}::"):
                continue
            for k in ("given", "followed", "partial", "rejected", "no_evidence"):
                tot[k] += int(e.get(k, 0))
            dropped += sum(int(x) for x in (e.get("dropped") or {}).values())
        total_n = next(
            (int(r["total"]) for r in rep.get("control_runs", []) if r["document_id"] == doc), 0
        )
        net = int(v["net"])
        mark = f"**{net:+d}**" if net > 0 else (f"{net:+d} ▼" if net < 0 else "±0")
        pairs = int(v["pairs"])
        pairs_txt = str(pairs) if pairs == trials else f"{pairs} ⚠"
        lines.append(
            f"| {doc} | {pairs_txt} | {v['control_hits']}/{total_n * pairs}"
            f" | {v['treat_hits']}/{total_n * pairs} | {mark} |"
            f" {tot['given']} / {dropped} / {tot['followed']} / {tot['partial']}"
            f" / {tot['rejected']} / {tot['no_evidence']} |"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--s1", type=Path)
    ap.add_argument("--s2", type=Path)
    ap.add_argument("--s3", type=Path)
    ap.add_argument("--md", type=Path, help="実測記録に貼る Markdown の出力先")
    args = ap.parse_args(argv)
    s1, s2, s3 = _load(args.s1), _load(args.s2), _load(args.s3)

    gates = {
        "G1": gate_g1(s1),
        "G2": gate_g2(s3),
        "G3": gate_g3(s2),
        "G4": gate_g4(s3),
    }
    all_ok = all(v[0] is True for v in gates.values())
    out: list[str] = []
    out.append("| ゲート | 判定 | 根拠 |")
    out.append("|---|---|---|")
    for g, (ok, why) in gates.items():
        mark = "✅ 通過" if ok else ("❌ 不通過" if ok is False else "— 判定不能")
        out.append(f"| {g} | {mark} | {why} |")
    out.append("")
    out.append("### 全体")
    out.append("")
    out.append("| アーム | 全体 exact match | McNemar（全項目） | McNemar（位置依存のみ） |")
    out.append("|---|---|---|---|")
    for arm, rep in (("S1", s1), ("S2", s2), ("S3", s3)):
        if rep is None:
            continue
        ma, mp = rep.get("mcnemar_all") or {}, rep.get("mcnemar_positional") or {}

        def _m(m: dict[str, Any]) -> str:
            p = m.get("p_value")
            return (
                f"{m.get('control_only')} / {m.get('treat_only')} / p = {p:.3f}"
                if p is not None
                else f"{m.get('control_only')} / {m.get('treat_only')} / —"
            )

        c, t = rep.get("control_exact_match"), rep.get("treat_exact_match")
        out.append(
            f"| {arm}（{rep.get('trials')} 試行） | {_f(c)} → {_f(t)} | {_m(ma)} | {_m(mp)} |"
        )
    out.append("")
    out.append("### 項目ごと（正解数／試行数、帳票合算）")
    out.append("")
    out.append(_md_field_table({"S1": s1, "S2": s2, "S3": s3}))
    for arm, rep in (("S2", s2), ("S3", s3)):
        if rep is None:
            continue
        out.append("")
        out.append(f"### {arm} 帳票ごと")
        out.append("")
        out.append(_md_doc_table(rep))
        out.append("")
        out.append(f"{arm} ヒントの扱い（介入アーム合計）: {_outcome_totals(rep)}")
        rows = (rep.get("hint_summary") or {}).get("affected_rows") or []
        if rows:
            out.append("")
            out.append(f"{arm} 落とした／捨てた項目と、その試行の正誤（対照 → 介入）:")
            out.append("")
            out.append("| 帳票 | 試行 | 項目 | 理由 | 対照 | 介入 |")
            out.append("|---|---|---|---|---|---|")
            for r in rows:
                out.append(
                    f"| {r['document_id']} | {r['trial']} | {r['field']} | {r['why']} |"
                    f" {'○' if r['control'] else '×'} | {'○' if r['treat'] else '×'} |"
                )
    text = "\n".join(out)
    print(text)
    print()
    print("総合:", "G1〜G4 すべて通過" if all_ok else "不通過あり（既定 off のまま）")
    if args.md:
        args.md.parent.mkdir(parents=True, exist_ok=True)
        args.md.write_text(text + "\n", encoding="utf-8")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
