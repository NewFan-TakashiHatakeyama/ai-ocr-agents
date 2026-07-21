"""sink.db_write ノード（DD-12 / §16 P6）の配線と安全策。

InMemory store + FakeWriter で、allowed_tables・台帳キー・on_failure・secret 解決を固定。
実 PG への書込み（ON CONFLICT の実挙動）は test_workflow_dbwrite_pg.py が担当。
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("langgraph.checkpoint.memory")

from langgraph.checkpoint.memory import MemorySaver  # noqa: E402

from newfan_orchestrator.consumer import InMemoryQueueConsumer  # noqa: E402
from newfan_orchestrator.workflow_graph import RunnerDeps  # noqa: E402
from newfan_orchestrator.workflow_runner import WorkflowRunner  # noqa: E402
from newfan_orchestrator.workflow_store import InMemoryWorkflowRunStore  # noqa: E402

TENANT = "ten_1"


def _graph(mode: str = "insert", on_failure: str = "halt_notify", **cfg: Any) -> dict[str, Any]:
    db_cfg: dict[str, Any] = {
        "connection_id": "con_pg",
        "table": "erp_demo.invoices",
        "mode": mode,
        "on_failure": on_failure,
        **cfg,
    }
    return {
        "version": 1,
        "nodes": [
            {"id": "t1", "type": "source.manual", "config": {}},
            {"id": "x1", "type": "process.extract", "config": {"schema_id": "sch_inv"}},
            {
                "id": "m1",
                "type": "transform.map_fields",
                "config": {
                    "mappings": [
                        {"from": "invoice_no", "to": "invoice_no"},
                        {"from": "total_amount", "to": "amount"},
                    ]
                },
            },
            {"id": "d1", "type": "sink.db_write", "config": db_cfg},
        ],
        "edges": [
            {"from": "t1", "to": "x1"},
            {"from": "x1", "to": "m1"},
            {"from": "m1", "to": "d1"},
        ],
    }


class FakeWriter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, list[dict[str, Any]]]] = []
        self.fail_times = 0

    def write(self, dsn: str, sql: str, rows: list[dict[str, Any]]) -> int:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("db down")
        self.calls.append((dsn, sql, rows))
        return len(rows)


def _env(
    graph: dict[str, Any],
    *,
    allowed: tuple[str, ...] = ("erp_demo.invoices",),
    secret_ref: str | None = "arn:aws:secretsmanager:xx:1:secret:conn/erp",
    with_resolver: bool = True,
) -> tuple[WorkflowRunner, InMemoryWorkflowRunStore, FakeWriter]:
    store = InMemoryWorkflowRunStore()
    store.seed_run("wfrun_d", tenant_id=TENANT, workflow_id="workflow_d", graph_json=graph)
    store.seed_db_connection(
        TENANT, "con_pg",
        config={"host": "erp.example", "dbname": "erp", "user": "sink"},
        secret_ref=secret_ref, allowed_tables=list(allowed),
    )
    writer = FakeWriter()
    runner = WorkflowRunner(
        store,
        InMemoryQueueConsumer(),
        RunnerDeps(
            store=store,
            send_webhook=lambda u, s, e: True,
            write_db=writer.write,
            resolve_secret=(lambda ref: f"resolved-{ref[-3:]}") if with_resolver else None,
        ),
        checkpointer=MemorySaver(),
    )
    return runner, store, writer


def _run_to_end(runner: WorkflowRunner, store: InMemoryWorkflowRunStore) -> str:
    runner.process({"type": "start", "tenant_id": TENANT, "workflow_run_id": "wfrun_d"})
    store.seed_extract_result(
        "run_wf_1",
        {
            "doc_type": "invoice",
            "fields": {
                "invoice_no": {"value": "GS0001", "confidence": 1.0},
                "total_amount": {"value": "7003", "confidence": 1.0},
            },
        },
    )
    return runner.process(
        {
            "type": "resume",
            "tenant_id": TENANT,
            "workflow_run_id": "wfrun_d",
            "event": {"kind": "extract_done", "run_id": "run_wf_1", "status": "confirmed"},
        }
    )


def test_insertは台帳キー付きプリペアドSQLで書き込む() -> None:
    runner, store, writer = _env(_graph())
    assert _run_to_end(runner, store) == "succeeded"
    dsn, sql, rows = writer.calls[0]
    # secret_ref が解決されて DSN に入る（config には秘密が無い）
    assert dsn == "postgresql://sink:resolved-erp@erp.example:5432/erp"
    assert sql == (
        'INSERT INTO "erp_demo"."invoices" ("invoice_no", "amount", "nf_write_key")'
        " VALUES (%(invoice_no)s, %(amount)s, %(nf_write_key)s)"
        ' ON CONFLICT ("nf_write_key") DO NOTHING'
    )
    # map_fields の出力 + 書込み台帳キー（workflow_run_id:node_id:行番号, §6.4）
    assert rows == [
        {"invoice_no": "GS0001", "amount": "7003", "nf_write_key": "wfrun_d:d1:0"}
    ]
    assert store.node_runs[("wfrun_d", "d1")]["status"] == "succeeded"
    assert store.node_runs[("wfrun_d", "d1")]["output"]["written"] == 1


def test_upsertは台帳キー無しでキー列DO_UPDATE() -> None:
    runner, store, writer = _env(_graph(mode="upsert", keys=["invoice_no"]))
    assert _run_to_end(runner, store) == "succeeded"
    _, sql, rows = writer.calls[0]
    assert 'ON CONFLICT ("invoice_no") DO UPDATE SET "amount" = EXCLUDED."amount"' in sql
    assert "nf_write_key" not in rows[0]


def test_allowed_tables外は書かずに失敗する() -> None:
    runner, store, writer = _env(_graph(), allowed=("erp_demo.other",))
    assert _run_to_end(runner, store) == "failed"
    assert writer.calls == []  # DD-12: 照合が先。書込みは一切走らない
    assert "allowed_tables" in store.runs["wfrun_d"]["error"]["message"]


def test_skip_and_notifyは失敗しても下流継続する() -> None:
    runner, store, writer = _env(_graph(on_failure="skip_and_notify"))
    writer.fail_times = 1
    assert _run_to_end(runner, store) == "succeeded"
    assert store.node_runs[("wfrun_d", "d1")]["status"] == "succeeded"
    assert store.node_runs[("wfrun_d", "d1")]["output"] == {"skipped": True}


def test_halt_notifyは失敗でrunをfailedにしretryで完走する() -> None:
    runner, store, writer = _env(_graph())
    writer.fail_times = 1
    assert _run_to_end(runner, store) == "failed"
    assert runner.process(
        {"type": "retry", "tenant_id": TENANT, "workflow_run_id": "wfrun_d"}
    ) == "succeeded"
    assert len(writer.calls) == 1


def test_secret_refがあるのにresolver未配線なら失敗する() -> None:
    runner, store, writer = _env(_graph(), with_resolver=False)
    assert _run_to_end(runner, store) == "failed"
    assert "resolver" in store.runs["wfrun_d"]["error"]["message"]
    assert writer.calls == []


def test_接続が無ければ失敗する() -> None:
    store = InMemoryWorkflowRunStore()
    store.seed_run("wfrun_d", tenant_id=TENANT, workflow_id="workflow_d", graph_json=_graph())
    runner = WorkflowRunner(
        store, InMemoryQueueConsumer(),
        RunnerDeps(store=store, send_webhook=lambda u, s, e: True, write_db=lambda *a: 0),
        checkpointer=MemorySaver(),
    )
    assert _run_to_end(runner, store) == "failed"
    assert "postgres 接続" in store.runs["wfrun_d"]["error"]["message"]


def test_mapを迂回した経路ではfieldsにfallbackせず失敗する() -> None:
    # dry-run で一度もプレビューされていない列（抽出フィールド名・mask 迂回）を
    # 顧客 DB に書かないため、db_write は map の出力が無ければ失敗する（レビューで修正）
    g = _graph()
    g["edges"] = [
        {"from": "t1", "to": "x1"},
        {"from": "x1", "to": "d1"},  # map を迂回
        {"from": "x1", "to": "m1"},
    ]
    runner, store, writer = _env(g)
    assert _run_to_end(runner, store) == "failed"
    assert writer.calls == []
    assert "map_fields" in store.runs["wfrun_d"]["error"]["message"]
