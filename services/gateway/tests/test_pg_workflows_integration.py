"""PgWorkflowsRepository を実 PostgreSQL で検証する（§16 P2）。

InMemory だけだと ORM/SQL と DDL の乖離を検出できない（correction_logs で実際に
500 を踏んだ）。版 increment・draft 戻し・audit 行・L009/L010 の参照解決を実 DB で固定する。

DATABASE_URL_TEST が設定されている時だけ動く（CI は workflow の postgres service）。
"""

from __future__ import annotations

import os
import uuid

import pytest

pytest.importorskip("sqlalchemy", reason="Pg リポジトリは runtime 依存")
pytest.importorskip("psycopg", reason="Pg リポジトリは runtime 依存")

from sqlalchemy import create_engine, text  # noqa: E402

_DSN = os.environ.get("DATABASE_URL_TEST")
pytestmark = pytest.mark.skipif(not _DSN, reason="DATABASE_URL_TEST 未設定（実 DB が要る）")

TENANT = "ten_wf_test"

GRAPH = {
    "version": 1,
    "nodes": [
        {"id": "t1", "type": "source.manual", "config": {}},
        {"id": "x1", "type": "process.extract", "config": {"schema_id": "sch_wf_test"}},
        {"id": "s1", "type": "sink.webhook", "config": {"connection_id": "con_wf_test"}},
    ],
    "edges": [{"from": "t1", "to": "x1"}, {"from": "x1", "to": "s1"}],
}


@pytest.fixture(scope="module")
def owner():
    return create_engine(_DSN, future=True)  # type: ignore[arg-type]


@pytest.fixture
def repo(owner):
    from newfan_gateway.db import PgWorkflowsRepository

    with owner.begin() as c:
        c.execute(
            text("INSERT INTO tenants (id, name) VALUES (:i,'wf test') ON CONFLICT DO NOTHING"),
            {"i": TENANT},
        )
    yield PgWorkflowsRepository(_DSN)  # type: ignore[arg-type]
    with owner.begin() as c:
        for table in ("audit_logs", "workflows", "field_schemas", "connections"):
            c.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": TENANT})


def _rec(name: str = "wf") -> "object":
    from newfan_gateway.records import WorkflowRecord

    return WorkflowRecord(
        id=f"workflow_{uuid.uuid4().hex[:12]}",
        tenant_id=TENANT,
        name=name,
        graph_json=GRAPH,
        created_by="tester",
    )


def test_作成と取得(repo) -> None:
    rec = repo.create_workflow(_rec())
    assert rec is not None and rec.version == 1 and rec.status == "draft"
    got = repo.get_workflow(TENANT, rec.id)
    assert got is not None
    assert got.graph_json["nodes"][0]["id"] == "t1"
    assert got.updated_at is not None


def test_更新は版が増えてdraftへ戻る(repo) -> None:
    rec = repo.create_workflow(_rec())
    repo.set_status(TENANT, rec.id, "active")
    updated = repo.update_workflow(TENANT, rec.id, graph_json=GRAPH, name="改名")
    assert updated is not None
    assert (updated.version, updated.status, updated.name) == (2, "draft", "改名")


def test_他テナントの行は触れない(repo) -> None:
    rec = repo.create_workflow(_rec())
    assert repo.get_workflow("ten_other", rec.id) is None
    assert repo.update_workflow("ten_other", rec.id, graph_json=GRAPH) is None
    assert repo.set_status("ten_other", rec.id, "active") is None


def test_schema_existsとconnection_okは実テーブルを見る(repo, owner) -> None:
    assert repo.schema_exists(TENANT, "sch_wf_test") is False
    assert repo.connection_ok(TENANT, "con_wf_test") is False
    with owner.begin() as c:
        c.execute(
            text(
                "INSERT INTO field_schemas (id, tenant_id, doc_type, version, fields)"
                " VALUES ('sch_wf_test', :t, 'invoice', 1, '[]'::jsonb)"
            ),
            {"t": TENANT},
        )
        c.execute(
            text(
                "INSERT INTO connections (id, tenant_id, type, name, config, status)"
                " VALUES ('con_wf_test', :t, 'webhook', 'hook', '{}'::jsonb, 'untested')"
            ),
            {"t": TENANT},
        )
    assert repo.schema_exists(TENANT, "sch_wf_test") is True
    # untested の接続は有効化に使わせない（§16.5）。tested に上げて初めて OK
    assert repo.connection_ok(TENANT, "con_wf_test") is False
    with owner.begin() as c:
        c.execute(
            text("UPDATE connections SET status='tested' WHERE id='con_wf_test' AND tenant_id=:t"),
            {"t": TENANT},
        )
    assert repo.connection_ok(TENANT, "con_wf_test") is True


def test_auditが記録される(repo, owner) -> None:
    rec = repo.create_workflow(_rec())
    repo.record_audit(
        TENANT, actor_id="sato", action="workflow.activate", target_id=rec.id,
        detail={"version": 1},
    )
    with owner.begin() as c:
        row = c.execute(
            text(
                "SELECT actor_type, actor_id, action, target_type, detail FROM audit_logs"
                " WHERE tenant_id=:t AND target_id=:i"
            ),
            {"t": TENANT, "i": rec.id},
        ).first()
    assert row is not None
    assert (row[0], row[1], row[2], row[3]) == ("human", "sato", "workflow.activate", "workflow")
    assert row[4]["version"] == 1
