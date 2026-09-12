"""golden/scripts/run_region_arms.sh の「前回の出力がある --out」の扱い。

前回の出力（<out>/<arm>_ab.json）がある --out にそのまま回すと、region_ab は（完全な出力
なら）1 件も抽出せずに同じ行を書き直し、前回の結果が新しい計測に化ける（mtime だけ新しく
なる。worker やプロンプトを変えた後でも見分けがつかない）。スクリプトは

- --resume なし: 止める（exit 2）
- --resume あり: 欠けた対が無いアームは回さず書き直さない。あるアームだけ region_ab --resume

を守る。region_from_gold / region_ab は差し替え（抽出はしない）、merge / missing_pairs の
ヒアドキュメントは本物の Python で回す。bash が無ければ飛ばす。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "golden" / "scripts" / "run_region_arms.sh"
ARMS = ("s3", "s2", "s1")
# 各アームの帳票数（golden/data/region_ab_<arm>.jsonl）
N_DOCS = {"s3": 12, "s2": 5, "s1": 21}


def _bash() -> str:
    found = shutil.which("bash")
    if found is None:
        pytest.skip("bash が無い")
    if sys.platform == "win32" and "git" not in found.lower():
        pytest.skip(f"Windows では Git Bash でだけ回す（見つかったのは {found}）")
    return found


_STUB_PY = """#!/usr/bin/env bash
# run_region_arms.sh の $PYTHON。region_from_gold / region_ab だけ差し替え、それ以外は本物へ
export PYTHONPATH="${PYTHONPATH:-}${EXTRA_PYTHONPATH:+${PATHSEP}${EXTRA_PYTHONPATH}}"
if [ "${1:-}" = "-m" ] && [ "${2:-}" = "newfan_golden.region_from_gold" ]; then
  shift 2; exec "$REAL_PY" "$STUB_DIR/fake_region_from_gold.py" "$@"
fi
if [ "${1:-}" = "-m" ] && [ "${2:-}" = "newfan_golden.region_ab" ]; then
  shift 2; exec "$REAL_PY" "$STUB_DIR/fake_region_ab.py" "$@"
fi
exec "$REAL_PY" "$@"
"""

_FAKE_FROM_GOLD = """import argparse, os, pathlib, sys
ap = argparse.ArgumentParser()
for k in ("--api", "--token", "--structure", "--spec"):
    ap.add_argument(k)
ap.add_argument("--out", type=pathlib.Path)
a = ap.parse_args()
with open(os.environ["STUB_LOG"], "a", encoding="utf-8") as fh:
    fh.write("region_from_gold " + " ".join(sys.argv[1:]) + "\\n")
a.out.write_text("{}", encoding="utf-8")
"""

# 抽出はせず、--gold × --trials × 2 アームのうち --resume に無い対を「正解 1/1」の行で埋めた
# 出力を書く。何対を新しく足したかを extracted に残す（テストが読む）
_FAKE_REGION_AB = """import argparse, json, os, pathlib, sys
from newfan_golden.dataset import load_jsonl
from newfan_golden.region_ab import missing_pairs, SCORING
ap = argparse.ArgumentParser()
for k in ("--regions", "--api", "--token"):
    ap.add_argument(k)
ap.add_argument("--gold", type=pathlib.Path)
ap.add_argument("--out", type=pathlib.Path)
ap.add_argument("--trials", type=int, default=5)
ap.add_argument("--timeout-sec", type=float, default=600.0)
ap.add_argument("--resume", type=pathlib.Path, default=None)
ap.add_argument("--allow-arm-reuse", action="store_true")
a = ap.parse_args()
with open(os.environ["STUB_LOG"], "a", encoding="utf-8") as fh:
    fh.write("region_ab " + " ".join(sys.argv[1:]) + "\\n")
resume = json.loads(a.resume.read_text(encoding="utf-8")) if a.resume else None
rep = dict(resume or {"scoring": SCORING, "control_runs": [], "treat_runs": []})
new = missing_pairs(load_jsonl(a.gold), a.trials, resume)
for doc_id, trial, arm in new:
    rep[f"{arm}_runs"].append({"document_id": doc_id, "trial": trial, "hits": 1, "total": 1,
                               "run_id": None, "field_hits": {"a": True}, "hints": None})
rep["trials"] = a.trials
rep["extracted"] = len(new)
a.out.write_text(json.dumps(rep), encoding="utf-8")
"""


class _Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.bash = _bash()
        self.stub = tmp_path / "stub"
        self.stub.mkdir()
        self.out = tmp_path / "out"
        self.log = self.stub / "calls.log"
        py = self.stub / "py"
        py.write_bytes(_STUB_PY.encode("utf-8"))  # LF のまま（bash が読む）
        py.chmod(0o755)
        (self.stub / "fake_region_from_gold.py").write_text(_FAKE_FROM_GOLD, encoding="utf-8")
        (self.stub / "fake_region_ab.py").write_text(_FAKE_REGION_AB, encoding="utf-8")
        self.env = dict(os.environ)
        self.env.update({
            "PYTHON": py.as_posix(),
            "REAL_PY": Path(sys.executable).as_posix(),
            "STUB_DIR": self.stub.as_posix(),
            "STUB_LOG": self.log.as_posix(),
            # pytest が見ている sys.path（newfan_schemas / httpx 等）を子にも渡す
            "EXTRA_PYTHONPATH": os.pathsep.join(p for p in sys.path if p),
            "PATHSEP": os.pathsep,
        })
        if b"\r\n" in SCRIPT.read_bytes():  # autocrlf の Windows 作業ツリー
            self.env["SHELLOPTS"] = "igncr"

    def run(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.bash, SCRIPT.as_posix(), "--api", "http://x", "--token", "t",
             "--out", self.out.as_posix(), *extra],
            cwd=ROOT, env=self.env, capture_output=True, encoding="utf-8", errors="replace",
        )

    def calls(self, name: str) -> list[str]:
        if not self.log.exists():
            return []
        return [ln for ln in self.log.read_text(encoding="utf-8").splitlines()
                if ln.startswith(name + " ")]

    def report(self, arm: str) -> dict[str, Any]:
        return dict(json.loads((self.out / f"{arm}_ab.json").read_text(encoding="utf-8")))

    def snapshot(self) -> dict[str, tuple[int, bytes]]:
        return {arm: ((p := self.out / f"{arm}_ab.json").stat().st_mtime_ns, p.read_bytes())
                for arm in ARMS}


def test_前回の出力がある_out_には_resume_を付けたときだけ回す(tmp_path: Path) -> None:
    h = _Harness(tmp_path)

    # 1. 新しい --out: 3 アームとも region_ab を --resume なしで回す
    r = h.run("--trials-s1", "2")
    assert r.returncode == 0, r.stderr
    ab = h.calls("region_ab")
    assert len(ab) == 3 and not any("--resume" in c for c in ab)
    assert len(h.calls("region_from_gold")) == 3
    assert h.report("s1")["extracted"] == N_DOCS["s1"] * 2 * 2
    assert h.report("s3")["extracted"] == N_DOCS["s3"] * 5 * 2
    before = h.snapshot()

    # 2. 同じ --out にそのまま回す: 止まる。何も回さず、出力も書き直さない
    r = h.run("--trials-s1", "2")
    assert r.returncode == 2
    assert "前回の出力があります" in r.stderr and "--resume" in r.stderr
    assert "別の --out" in r.stderr
    assert len(h.calls("region_ab")) == 3 and len(h.calls("region_from_gold")) == 3
    assert h.snapshot() == before

    # 3. --resume を付ける。欠けた対が無いアームは回さず、書き直さない（mtime も変わらない）
    r = h.run("--trials-s1", "2", "--resume")
    assert r.returncode == 0, r.stderr
    assert len(h.calls("region_ab")) == 3
    assert r.stdout.count("[skip] 前回の出力に欠けた対はありません") == 3
    assert h.snapshot() == before
    assert not (h.out / "s2_ab_prefill.json").exists()

    # 4. S2 の介入の対を 1 つ落とす（LLM 側の失敗で捨てた試行の形）→ S2 だけ region_ab --resume
    s2 = h.report("s2")
    dropped = s2["treat_runs"].pop(0)
    (h.out / "s2_ab.json").write_text(json.dumps(s2), encoding="utf-8")
    r = h.run("--trials-s1", "2", "--resume")
    assert r.returncode == 0, r.stderr
    ab = h.calls("region_ab")
    assert len(ab) == 4
    assert "--gold golden/data/region_ab_s2.jsonl" in ab[-1]
    assert f"--resume {h.out.as_posix()}/s2_ab_prefill.json" in ab[-1]
    assert "[resume] 前回の出力から欠けた対 1 件だけ回す" in r.stdout
    assert r.stdout.count("[skip] 前回の出力に欠けた対はありません") == 2  # S3 / S1 は回さない
    assert (h.out / "s2_ab_prefill.json").exists()
    s2 = h.report("s2")
    assert s2["extracted"] == 1
    assert any(x["document_id"] == dropped["document_id"] and x["trial"] == dropped["trial"]
               for x in s2["treat_runs"])
    after = h.snapshot()
    assert after["s3"] == before["s3"] and after["s1"] == before["s1"]

    # 5. S1 の試行を 2 → 3 に延ばす: S1 だけ region_ab --resume（欠けた対 = 21 帳票 × 2 アーム）
    r = h.run("--trials-s1", "3", "--resume")
    assert r.returncode == 0, r.stderr
    ab = h.calls("region_ab")
    assert len(ab) == 5
    assert "--gold golden/data/region_ab_s1.jsonl" in ab[-1] and "--trials 3" in ab[-1]
    assert "--resume" in ab[-1]
    assert "欠けた対 42 件" in r.stdout
    assert r.stdout.count("[skip] 前回の出力に欠けた対はありません") == 2  # S3 / S2 は回さない
    assert h.report("s1")["extracted"] == N_DOCS["s1"] * 2
    assert h.report("s1")["trials"] == 3


def test_arms_で回すアームを絞る(tmp_path: Path) -> None:
    """--arms s3: S3 だけ回す（決定論で S3 にしか届かない変更の測り直し）。他のアームは
    領域も起こさず出力も作らない。不明なアームは止める。"""
    h = _Harness(tmp_path)
    r = h.run("--arms", "s3")
    assert r.returncode == 0, r.stderr
    ab = h.calls("region_ab")
    assert len(ab) == 1 and "--gold golden/data/region_ab_s3.jsonl" in ab[0]
    assert len(h.calls("region_from_gold")) == 1
    assert (h.out / "s3_ab.json").exists()
    assert not (h.out / "s2_ab.json").exists() and not (h.out / "s1_ab.json").exists()
    # 同じ --out に --arms s2 は「前回の出力」に当たらない（S2 の出力は無い）ので回る
    r = h.run("--arms", "s2")
    assert r.returncode == 0, r.stderr
    assert len(h.calls("region_ab")) == 2
    # S3 をもう一度は止まる（前回の出力がある）
    r = h.run("--arms", "s3")
    assert r.returncode == 2 and "前回の出力があります" in r.stderr
    r = h.run("--arms", "s4")
    assert r.returncode == 2 and "不明なアーム" in r.stderr
