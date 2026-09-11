"""第 3 回計測のゲート判定（golden/scripts/eval_gates_v2.py）。

敵対的レビューで指摘された 3 点を固定する:
- 分母がアームごとにずれた項目（"4/4" 対 "4/5"）を生の正解数で比べない（G3 は対で見る）
- 対が試行数に満たない帳票は「通過」ではなく「判定不能」（G2・G3）
- 片方のアームが全滅して率が None でも落ちない
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "eval_gates_v2", Path(__file__).resolve().parents[1] / "scripts" / "eval_gates_v2.py"
)
assert _SPEC is not None and _SPEC.loader is not None
eg = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(eg)


def _run(doc: str, trial: int, hits: dict[str, bool], hints: dict[str, Any] | None = None) -> dict:
    return {
        "document_id": doc,
        "trial": trial,
        "hits": sum(hits.values()),
        "total": len(hits),
        "run_id": None,
        "field_hits": hits,
        "hints": hints,
    }


def _report(control: list[dict], treat: list[dict], trials: int, **extra: Any) -> dict[str, Any]:
    from newfan_golden.region_ab import Tally, hint_summary, mcnemar, per_doc_net

    t = Tally()
    for arm, rows in (("control", control), ("treat", treat)):
        for r in rows:
            for name, hit in r["field_hits"].items():
                t.note(r["document_id"], name, False, arm, hit, r["trial"])
    fields = [x.as_dict() for x in t.per_field.values()]
    rep = {
        "trials": trials,
        "control_exact_match": 0.5,
        "treat_exact_match": 0.5,
        "control_runs": control,
        "treat_runs": treat,
        "fields": fields,
        "mcnemar_all": mcnemar(t.paired),
        "mcnemar_positional": mcnemar(t.paired, only=set()),
        "per_doc_net": per_doc_net(control, treat),
        "hint_summary": hint_summary(control, treat),
    }
    rep.update(extra)
    return rep


class TestG3:
    def test_対照の試行が_1_つ失敗しても生の正解数で通さない(self) -> None:
        """対照 4/4・介入 4/5（介入が 1 回外した）。生の 4 ≥ 4 で通してはいけない。"""
        control = [_run("s2_sample8", i, {"customer_name": True}) for i in range(4)]
        treat = [
            _run(
                "s2_sample8",
                i,
                {"customer_name": i != 2},
                {
                    "given": ["customer_name"],
                    "dropped": {},
                    "outcomes": {"customer_name": "followed"},
                },
            )
            for i in range(5)
        ]
        ok, why = eg.gate_g3(_report(control, treat, 5))
        assert ok is None
        assert "判定不能" in why

    def test_対で対照以上なら通る(self) -> None:
        control = [_run("s2_sample8", i, {"customer_name": i < 3}) for i in range(5)]
        treat = [
            _run(
                "s2_sample8",
                i,
                {"customer_name": i < 3},
                {
                    "given": ["customer_name"],
                    "dropped": {},
                    "outcomes": {"customer_name": "rejected"},
                },
            )
            for i in range(5)
        ]
        ok, _ = eg.gate_g3(_report(control, treat, 5))
        assert ok is True

    def test_kind_conflict_で_4_回守れば介入が下でも通る(self) -> None:
        control = [_run("s2_sample8", i, {"customer_name": True}) for i in range(5)]
        treat = [
            _run(
                "s2_sample8",
                i,
                {"customer_name": i != 0},
                {"given": [], "dropped": {"customer_name": "kind_conflict"}, "outcomes": {}},
            )
            for i in range(5)
        ]
        ok, why = eg.gate_g3(_report(control, treat, 5))
        assert ok is True
        assert "守った回数 5/5" in why

    def test_文字が無くて届かなかっただけでは守ったに数えない(self) -> None:
        control = [_run("s2_sample8", i, {"customer_name": True}) for i in range(5)]
        treat = [
            _run(
                "s2_sample8",
                i,
                {"customer_name": i != 0},
                {"given": [], "dropped": {"customer_name": "no_spans_in_region"}, "outcomes": {}},
            )
            for i in range(5)
        ]
        ok, why = eg.gate_g3(_report(control, treat, 5))
        assert ok is False
        assert "届かなかった 5" in why


class TestG2:
    def _s3(self, treat_trials: int, lose: bool) -> dict[str, Any]:
        control = [_run("s3_x", i, {"a": True, "b": True}) for i in range(5)]
        treat = [_run("s3_x", i, {"a": True, "b": not lose}) for i in range(treat_trials)]
        return _report(control, treat, 5)

    def test_対が_5_に満たない帳票は判定不能(self) -> None:
        ok, why = eg.gate_g2(self._s3(2, lose=True))
        assert ok is None
        assert "判定不能" in why and "'s3_x': 2" in why

    def test_介入が全滅した帳票も判定不能(self) -> None:
        ok, why = eg.gate_g2(self._s3(0, lose=True))
        assert ok is None
        assert "'s3_x': 0" in why

    def test_純減が閾値以内なら通る(self) -> None:
        rep = self._s3(5, lose=False)
        ok, _ = eg.gate_g2(rep)
        assert ok is True

    def test_純減が閾値を超えると落ちる(self) -> None:
        ok, why = eg.gate_g2(self._s3(5, lose=True))  # 5 試行すべて 1 項目落とす → −5
        assert ok is False
        assert "下回る帳票" in why


def test_率が_None_でも表を出せる(tmp_path: Path) -> None:
    rep = _report([_run("s1_d", 0, {"a": True})], [], 1)
    rep["treat_exact_match"] = None
    p = tmp_path / "s1.json"
    p.write_text(json.dumps(rep, ensure_ascii=False), encoding="utf-8")
    md = tmp_path / "g.md"
    rc = eg.main(["--s1", str(p), "--md", str(md)])
    assert rc == 1  # 通過ではない
    assert "0.500 → —" in md.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "ch,ct,th,tt,mark",
    [(4, 4, 4, 5, "▼"), (4, 5, 4, 4, "**"), (3, 5, 3, 5, "")],
)
def test_項目表の印は率で決める(ch: int, ct: int, th: int, tt: int, mark: str) -> None:
    rep = {
        "fields": [
            {
                "name": "d::a",
                "control": f"{ch}/{ct}",
                "treat": f"{th}/{tt}",
                "position_dependent": False,
            }
        ]
    }
    table = eg._md_field_table({"S1": rep})
    cell = table.splitlines()[-1]
    if mark:
        assert mark in cell
    else:
        assert "▼" not in cell and "**" not in cell
