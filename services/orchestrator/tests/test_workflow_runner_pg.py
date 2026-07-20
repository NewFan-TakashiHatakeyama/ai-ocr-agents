"""ワークフロー層 × 実 PostgreSQL（§16 設計 v0.2 §16 の実測 4 項目のテスト昇格）。

scripts/probe_langgraph_workflow.py で実測した以下を回帰として固定する:
1. checkpointer のスキーマ分離（lg_wf / public）
2. 非所有ロール（RLS 運用）での PostgresSaver
3. プロセス跨ぎ resume（saver を作り直して resume）
3'. 再実行境界（完了済みノードは再実行されず、interrupt 前コードは再実行される）

DATABASE_URL_TEST が設定されている時だけ動く。
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest

pytest.importorskip("langgraph.checkpoint.postgres")
pytest.importorskip("psycopg")

from langgraph.checkpoint.postgres import PostgresSaver  # noqa: E402

_DSN = os.environ.get("DATABASE_URL_TEST")
pytestmark = pytest.mark.skipif(not _DSN, reason="DATABASE_URL_TEST 未設定（実 DB が要る）")

SCHEMA = "lg_wf"


def _plain(dsn: str) -> str:
    return dsn.replace("+psycopg", "")


def _wf_dsn(dsn: str) -> str:
    plain = _plain(dsn)
    sep = "&" if "?" in plain else "?"
    return f"{plain}{sep}options=-csearch_path%3D{SCHEMA}"


@pytest.fixture(scope="module", autouse=True)
def _schema() -> None:
    import psycopg

    with psycopg.connect(_plain(_DSN), autocommit=True) as conn:  # type: ignore[arg-type]
        conn.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    with PostgresSaver.from_conn_string(_wf_dsn(_DSN)) as saver:  # type: ignore[arg-type]
        saver.setup()


def test_lg_wfとpublicのcheckpointは分離されている() -> None:
    thread = f"sep_{uuid.uuid4().hex[:8]}"
    cfg = {"configurable": {"thread_id": thread, "checkpoint_ns": ""}}
    blank = {
        "v": 4, "id": "c1", "ts": "2026-07-21T00:00:00+00:00",
        "channel_values": {}, "channel_versions": {}, "versions_seen": {},
        "pending_sends": [],
    }
    with PostgresSaver.from_conn_string(_wf_dsn(_DSN)) as wf:  # type: ignore[arg-type]
        wf.put(cfg, blank, {"source": "input", "step": 0}, {})
        assert wf.get(cfg) is not None
    with PostgresSaver.from_conn_string(_plain(_DSN)) as pub:  # type: ignore[arg-type]
        # 抽出グラフ側（public）から同じ thread は見えない＝レイヤ分離（DD-11）
        assert pub.get(cfg) is None


def _tiny_graph(counters: dict[str, int]) -> Any:
    from typing import TypedDict

    from langgraph.graph import END, START, StateGraph
    from langgraph.types import interrupt

    class S(TypedDict, total=False):
        approved: str

    def pre(state: S) -> dict:
        counters["pre"] = counters.get("pre", 0) + 1
        return {}

    def gate(state: S) -> dict:
        counters["gate_pre_interrupt"] = counters.get("gate_pre_interrupt", 0) + 1
        answer = interrupt({"kind": "await_hitl"})
        return {"approved": str(answer)}

    g = StateGraph(S)
    g.add_node("pre", pre)
    g.add_node("gate", gate)
    g.add_edge(START, "pre")
    g.add_edge("pre", "gate")
    g.add_edge("gate", END)
    return g


def test_saverを作り直してもresumeでき再実行境界が保たれる() -> None:
    from langgraph.types import Command

    counters: dict[str, int] = {}
    thread = f"resume_{uuid.uuid4().hex[:8]}"
    cfg = {"configurable": {"thread_id": thread}}

    with PostgresSaver.from_conn_string(_wf_dsn(_DSN)) as saver:  # type: ignore[arg-type]
        app = _tiny_graph(counters).compile(checkpointer=saver)
        result = app.invoke({}, cfg)
        assert "__interrupt__" in result
    assert counters == {"pre": 1, "gate_pre_interrupt": 1}

    # saver / compiled graph を作り直す＝プロセス再起動の相当（checkpoint だけが共有）
    with PostgresSaver.from_conn_string(_wf_dsn(_DSN)) as saver2:  # type: ignore[arg-type]
        app2 = _tiny_graph(counters).compile(checkpointer=saver2)
        result = app2.invoke(Command(resume="ok"), cfg)
    assert result.get("approved") == "ok"
    # 完了済みノードは再実行されない / interrupt 前コードは再実行される（冪等設計の根拠）
    assert counters["pre"] == 1
    assert counters["gate_pre_interrupt"] == 2


def test_非所有ロールでcheckpointを読み書きできる() -> None:
    # RLS 運用（§7.3）でアプリは newfan_app 相当の非所有ロールになる。
    # CI/ローカルにも同条件のロールを作って PostgresSaver が動くことを固定する。
    import psycopg

    with psycopg.connect(_plain(_DSN), autocommit=True) as conn:  # type: ignore[arg-type]
        conn.execute(
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='wf_app_test')"
            " THEN CREATE ROLE wf_app_test LOGIN PASSWORD 'wf_app_pw'; END IF; END $$;"
        )
        conn.execute("ALTER ROLE wf_app_test NOBYPASSRLS")
        conn.execute(f"GRANT USAGE ON SCHEMA {SCHEMA} TO wf_app_test")
        conn.execute(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {SCHEMA}"
            " TO wf_app_test"
        )
        host = conn.info.host
        port = conn.info.port
        db = conn.info.dbname

    app_dsn = (
        f"host={host} port={port} dbname={db} user=wf_app_test password=wf_app_pw"
        f" options='-csearch_path={SCHEMA}'"
    )
    thread = f"approle_{uuid.uuid4().hex[:8]}"
    cfg = {"configurable": {"thread_id": thread, "checkpoint_ns": ""}}
    blank = {
        "v": 4, "id": "c1", "ts": "2026-07-21T00:00:00+00:00",
        "channel_values": {}, "channel_versions": {}, "versions_seen": {},
        "pending_sends": [],
    }
    with PostgresSaver.from_conn_string(app_dsn) as saver:
        saver.put(cfg, blank, {"source": "input", "step": 0}, {})
        assert saver.get(cfg) is not None
