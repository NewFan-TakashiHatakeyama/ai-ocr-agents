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

    # 本番は scripts/setup_checkpointer.py が public / lg_wf の両方を作る。CI の
    # postgres は alembic しか流していないため、ここで両方を用意する
    # （public 側が無いと分離テストの「public から見えない」確認ができない）。
    with PostgresSaver.from_conn_string(_plain(_DSN)) as saver:  # type: ignore[arg-type]
        saver.setup()
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


def test_lock_run保持中でもnode_run_startがブロックしない() -> None:
    """実 AWS で踏んだ自己ブロックの回帰。

    lock_run が FOR UPDATE だと、保持中の workflow_node_runs INSERT（別トランザクション）が
    FK 検査（参照行の FOR KEY SHARE）で自分のロックを待って永久に固まる。
    FOR NO KEY UPDATE は KEY SHARE と競合しないため通る。
    """
    import threading

    from sqlalchemy import create_engine, text

    from newfan_orchestrator.workflow_store import PgWorkflowRunStore

    owner = create_engine(_DSN, future=True)  # type: ignore[arg-type]
    tenant = "ten_wf_lock"
    wf_id = f"workflow_{uuid.uuid4().hex[:12]}"
    run_id = f"wfrun_{uuid.uuid4().hex[:12]}"
    with owner.begin() as c:
        c.execute(text("INSERT INTO tenants (id,name) VALUES (:t,'x') ON CONFLICT DO NOTHING"),
                  {"t": tenant})
        c.execute(text(
            "INSERT INTO workflows (id, tenant_id, name, graph_json) VALUES (:w,:t,'wf','{}')"),
            {"w": wf_id, "t": tenant})
        c.execute(text(
            "INSERT INTO workflow_runs (id, tenant_id, workflow_id, workflow_version, trigger)"
            " VALUES (:r,:t,:w,1,'{}')"), {"r": run_id, "t": tenant, "w": wf_id})

    store = PgWorkflowRunStore(_DSN, enqueue=lambda s, m: None)  # type: ignore[arg-type]
    done = threading.Event()

    def insert_node_run() -> None:
        store.node_run_start(tenant, run_id, "x1", "process.extract")
        done.set()

    try:
        with store.lock_run(tenant, run_id) as locked:
            assert locked is not None
            t = threading.Thread(target=insert_node_run, daemon=True)
            t.start()
            # FOR UPDATE だとここが永久に待つ（10 秒で検知して失敗させる）
            assert done.wait(timeout=10), "node_run_start が run 行ロックでブロックした"
            locked.update(status="running", waiting=None)
    finally:
        with owner.begin() as c:
            c.execute(text("DELETE FROM workflow_node_runs WHERE workflow_run_id=:r"), {"r": run_id})
            c.execute(text("DELETE FROM workflow_runs WHERE id=:r"), {"r": run_id})
            c.execute(text("DELETE FROM workflows WHERE id=:w"), {"w": wf_id})
            c.execute(text("DELETE FROM tenants WHERE id=:t"), {"t": tenant})


def test_ensure_extract_runは実DDLに対して冪等に動く() -> None:
    """実 DDL への INSERT の回帰。engine_versions が NOT NULL（DEFAULT なし）で、
    省略すると NotNullViolation になる（実 AWS の E2E で検出）。冪等キーの再利用も固定する。
    """
    from sqlalchemy import create_engine, text

    from newfan_orchestrator.workflow_store import PgWorkflowRunStore

    owner = create_engine(_DSN, future=True)  # type: ignore[arg-type]
    tenant = "ten_wf_ext"
    doc_id = f"doc_{uuid.uuid4().hex[:12]}"
    with owner.begin() as c:
        c.execute(text("INSERT INTO tenants (id,name) VALUES (:t,'x') ON CONFLICT DO NOTHING"),
                  {"t": tenant})
        c.execute(text(
            "INSERT INTO documents (id, tenant_id, storage_uri, mime_type, page_count, status)"
            " VALUES (:d,:t,'s3://x','image/png',1,'uploaded')"), {"d": doc_id, "t": tenant})

    enqueued: list[dict[str, Any]] = []
    store = PgWorkflowRunStore(  # type: ignore[arg-type]
        _DSN, enqueue=lambda s, m: enqueued.append({"stream": s, **m})
    )
    try:
        notify = {"stream": "q.workflow", "workflow_run_id": "wfrun_x"}
        r1 = store.ensure_extract_run(tenant, doc_id, None, {"force_vl": False},
                                      "wfrun_x:x1", notify)
        r2 = store.ensure_extract_run(tenant, doc_id, None, {"force_vl": False},
                                      "wfrun_x:x1", notify)
        assert r1 == r2, "冪等キーが効いていない（二重 Run）"
        assert len(enqueued) == 1
        assert enqueued[0]["notify"] == notify
        with owner.begin() as c:
            row = c.execute(text(
                "SELECT engine_versions, options->>'workflow_idem', status FROM extraction_runs"
                " WHERE id=:r"), {"r": r1}).first()
        assert row is not None
        assert row[1] == "wfrun_x:x1"
    finally:
        with owner.begin() as c:
            c.execute(text("DELETE FROM jobs WHERE tenant_id=:t"), {"t": tenant})
            c.execute(text("DELETE FROM extraction_runs WHERE tenant_id=:t"), {"t": tenant})
            c.execute(text("DELETE FROM documents WHERE id=:d"), {"d": doc_id})
            c.execute(text("DELETE FROM tenants WHERE id=:t"), {"t": tenant})


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


def test_load_extract_resultは実DDLからoriginal_nameとfieldsを返す() -> None:
    """敵対的レビュー確定所見（Pg経路のテストゼロ）の回帰。

    分類ゲート（⑦）の入力になる original_name の SELECT 列追加が実 DDL と
    整合していることを InMemory でなく実 PostgreSQL で固定する。
    """
    from sqlalchemy import create_engine, text

    from newfan_orchestrator.workflow_store import PgWorkflowRunStore

    owner = create_engine(_DSN, future=True)  # type: ignore[arg-type]
    tenant = "ten_wf_ler"
    doc_id = f"doc_{uuid.uuid4().hex[:12]}"
    with owner.begin() as c:
        c.execute(text("INSERT INTO tenants (id,name) VALUES (:t,'x') ON CONFLICT DO NOTHING"),
                  {"t": tenant})
        c.execute(text(
            "INSERT INTO documents (id, tenant_id, storage_uri, original_name, mime_type,"
            " page_count, doc_type, status)"
            " VALUES (:d,:t,'s3://x','請求書_ABC_2026.pdf','image/png',1,'invoice','uploaded')"),
            {"d": doc_id, "t": tenant})

    store = PgWorkflowRunStore(_DSN, enqueue=lambda s, m: None)  # type: ignore[arg-type]
    run_id = store.ensure_extract_run(
        tenant, doc_id, None, {"force_vl": False}, f"{doc_id}:x1",
        {"stream": "q.workflow", "workflow_run_id": "wfrun_ler"},
    )
    try:
        with owner.begin() as c:
            c.execute(text(
                "INSERT INTO extraction_fields (id, tenant_id, run_id, field_name,"
                " value_normalized, final_value, confidence)"
                " VALUES (:i,:t,:r,'total_amount','136998',NULL,0.97)"),
                {"i": f"fld_{uuid.uuid4().hex[:12]}", "r": run_id, "t": tenant})

        got = store.load_extract_result(tenant, run_id)
        assert got["doc_type"] == "invoice"
        assert got["original_name"] == "請求書_ABC_2026.pdf"
        assert got["fields"]["total_amount"]["value"] == "136998"
        assert got["run_confidence"] == pytest.approx(0.97, abs=1e-6)
    finally:
        with owner.begin() as c:
            c.execute(text("DELETE FROM extraction_fields WHERE run_id=:r"), {"r": run_id})
            c.execute(text("DELETE FROM jobs WHERE ref_id=:r"), {"r": run_id})
            c.execute(text("DELETE FROM extraction_runs WHERE id=:r"), {"r": run_id})
            c.execute(text("DELETE FROM documents WHERE id=:d"), {"d": doc_id})
            c.execute(text("DELETE FROM tenants WHERE id=:t"), {"t": tenant})
