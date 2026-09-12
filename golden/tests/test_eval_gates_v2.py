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


class TestG4:
    """第 4 回から: 落とした／捨てた対の McNemar が「介入が有意に悪い」でなければ通す。

    第 3 回までの「差が 0 以上」は、効果が無ければ差が 0 を中心に散らばるので、効果が
    無くても半分の確率で落ちた（設計 §3 の注記）。
    """

    @staticmethod
    def _s3(outcomes: list[tuple[bool, bool]]) -> dict[str, Any]:
        """(対照の正誤, 介入の正誤) の列を、5 帳票 × 5 試行 × 4 項目に順に割り付ける。
        介入アームは全項目を rejected（捨てた）にして affected の対にする。"""
        fields = ["a", "b", "c", "d"]
        control: list[dict] = []
        treat: list[dict] = []
        k = 0
        for d in range(5):
            for t in range(5):
                ch: dict[str, bool] = {}
                th: dict[str, bool] = {}
                for f in fields:
                    if k >= len(outcomes):
                        break
                    ch[f], th[f] = outcomes[k]
                    k += 1
                if not ch:
                    continue
                control.append(_run(f"s3_d{d}", t, ch))
                treat.append(
                    _run(
                        f"s3_d{d}",
                        t,
                        th,
                        {"given": list(th), "dropped": {}, "outcomes": {f: "rejected" for f in th}},
                    )
                )
        return _report(control, treat, 5)

    def test_100_対で差が_マイナス1_なら通る(self) -> None:
        """効果が無いときの揺れ（30 対 29）。旧規則「差 ≥ 0」では落ちていた。"""
        rows = [(True, False)] * 30 + [(False, True)] * 29 + [(True, True)] * 41
        ok, why = eg.gate_g4(self._s3(rows))
        assert ok is True
        assert "100 対" in why and "差 -1" in why
        assert "対照のみ 30 / 介入のみ 29" in why and "有意差なし" in why

    def test_20_対すべて対照だけ正解なら落ちる(self) -> None:
        ok, why = eg.gate_g4(self._s3([(True, False)] * 20))
        assert ok is False
        assert "介入が有意に悪い" in why and "差 -20" in why

    def test_対が_0_なら判定不能(self) -> None:
        ok, why = eg.gate_g4(self._s3([]))
        assert ok is None
        assert "判定材料なし" in why

    def test_S3_が無ければ判定不能(self) -> None:
        assert eg.gate_g4(None) == (None, "S3 の結果が無い")


def test_片方のアームを再利用した計測は先頭で警告する(tmp_path: Path, capsys: Any) -> None:
    """region_ab --allow-arm-reuse の出力（resume_arm_reuse: true）が 1 つでも入力にあれば、
    表の先頭（stdout と Markdown の両方）に警告行を出す。無ければ出さない。
    どちらのアームを何帳票再利用したかは resume_arm_reuse_docs から出す（対照とは限らない）。"""
    plain = _report([_run("s2_d", 0, {"a": True})], [_run("s2_d", 0, {"a": True})], 1)
    reused = _report([_run("s3_d", 0, {"a": True})], [_run("s3_d", 0, {"a": True})], 1,
                     resume_arm_reuse=True, resume_arm_reuse_docs={"s3_d": "control"})
    p2, p3, md = tmp_path / "s2.json", tmp_path / "s3.json", tmp_path / "g.md"
    p2.write_text(json.dumps(plain, ensure_ascii=False), encoding="utf-8")
    p3.write_text(json.dumps(reused, ensure_ascii=False), encoding="utf-8")

    eg.main(["--s2", str(p2), "--s3", str(p3), "--md", str(md)])
    text = md.read_text(encoding="utf-8")
    first = text.splitlines()[0]
    assert first.startswith("⚠ 片方のアームを再利用した計測（時間帯の交絡あり）")
    assert ": S3（対照 1 帳票）" in first and "S2" not in first
    assert "対照アーム" not in first
    assert capsys.readouterr().out.splitlines()[0] == first

    eg.main(["--s2", str(p2), "--md", str(md)])
    assert "⚠" not in md.read_text(encoding="utf-8")
    assert "⚠" not in capsys.readouterr().out


def test_再利用したのが介入アームでもそのとおりに書く(tmp_path: Path) -> None:
    """介入を残して対照だけ回し直した出力（resume_arm_reuse_docs の値が treat）を、
    「対照アームを再利用」と書かない。帳票の内訳が無い古い出力はアーム名だけ。"""
    treat_kept = _report([_run("s3_a", 0, {"a": True}), _run("s3_b", 0, {"a": True})],
                         [_run("s3_a", 0, {"a": True}), _run("s3_b", 0, {"a": True})], 1,
                         resume_arm_reuse=True,
                         resume_arm_reuse_docs={"s3_a": "treat", "s3_b": "treat"})
    mixed = _report([_run("s1_a", 0, {"a": True})], [_run("s1_a", 0, {"a": True})], 1,
                    resume_arm_reuse=True,
                    resume_arm_reuse_docs={"s1_a": "control", "s1_b": "treat", "s1_c": "control"})
    old = _report([_run("s2_a", 0, {"a": True})], [_run("s2_a", 0, {"a": True})], 1,
                  resume_arm_reuse=True)
    p1, p2, p3, md = (tmp_path / n for n in ("s1.json", "s2.json", "s3.json", "g.md"))
    p1.write_text(json.dumps(mixed, ensure_ascii=False), encoding="utf-8")
    p2.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
    p3.write_text(json.dumps(treat_kept, ensure_ascii=False), encoding="utf-8")
    eg.main(["--s1", str(p1), "--s2", str(p2), "--s3", str(p3), "--md", str(md)])
    first = md.read_text(encoding="utf-8").splitlines()[0]
    assert ": S1（対照 2 帳票、介入 1 帳票）, S2, S3（介入 2 帳票）" in first
    assert eg._arm_reuse_label("S3", treat_kept) == "S3（介入 2 帳票）"


def test_総合行は既定_on_を前提に書く(tmp_path: Path, capsys: Any) -> None:
    """ヒントは第 3 回の結果で既定 on（196b720）。不通過があっても「既定 off のまま」とは
    書かない（そう読んだ運用者が「何もしなくてよい」と受け取る）。"""
    rep = _report([_run("s1_d", 0, {"a": True})], [_run("s1_d", 0, {"a": True})], 1)
    p = tmp_path / "s1.json"
    p.write_text(json.dumps(rep, ensure_ascii=False), encoding="utf-8")
    assert eg.main(["--s1", str(p)]) == 1
    out = capsys.readouterr().out
    assert "総合: 不通過あり（既定 on の根拠を見直す）" in out
    assert "既定 off" not in out


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
