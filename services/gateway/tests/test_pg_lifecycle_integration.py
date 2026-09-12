"""接続の無効化/削除・スキーマのアーカイブ・ワークフローの削除を実 PostgreSQL で検証する（C9-D）。

InMemory だと検出できないもの:
- jsonb の参照検索（graph_json / trigger.graph_json のノードを引く SQL）
- DELETE 文に組み込んだ参照ガード（NOT EXISTS）と FK（workflow_runs.workflow_id）
- is_active の集約（版が複数ある doc_type のアーカイブ判定）

DATABASE_URL_TEST が設定されている時だけ動く（CI/ローカルは compose の postgres）。
"""

from __future__ import annotations

import json
import os
import uuid

import pytest

pytest.importorskip("sqlalchemy", reason="Pg リポジトリは runtime 依存")
pytest.importorskip("psycopg", reason="Pg リポジトリは runtime 依存")

from sqlalchemy import create_engine, text  # noqa: E402

_DSN = os.environ.get("DATABASE_URL_TEST")
pytestmark = pytest.mark.skipif(not _DSN, reason="DATABASE_URL_TEST 未設定（実 DB が要る）")

TENANT = "ten_lifecycle_test"
OTHER = "ten_lifecycle_other"

# テストごとに一意な id を使う（並行実行・前回の残骸と衝突させない）
_CON = f"con_lc_{uuid.uuid4().hex[:8]}"
_SCH = f"sch_lc_{uuid.uuid4().hex[:8]}"


def _graph(connection_id: str = _CON, schema_id: str = _SCH) -> dict:
    return {
        "version": 1,
        "nodes": [
            {"id": "t1", "type": "source.manual", "config": {}},
            {"id": "x1", "type": "process.extract", "config": {"schema_id": schema_id}},
            {"id": "s1", "type": "sink.webhook", "config": {"connection_id": connection_id}},
        ],
        "edges": [{"from": "t1", "to": "x1"}, {"from": "x1", "to": "s1"}],
    }


@pytest.fixture(scope="module")
def owner():
    return create_engine(_DSN, future=True)  # type: ignore[arg-type]


@pytest.fixture
def repos(owner):
    from newfan_gateway.db import PgAdminRepository, PgWorkflowsRepository

    with owner.begin() as c:
        for t in (TENANT, OTHER):
            c.execute(
                text("INSERT INTO tenants (id, name) VALUES (:i,'lifecycle') ON CONFLICT DO NOTHING"),
                {"i": t},
            )
    yield PgAdminRepository(_DSN), PgWorkflowsRepository(_DSN)  # type: ignore[arg-type]
    with owner.begin() as c:
        for table in (
            "audit_logs", "workflow_node_runs", "workflow_runs", "workflows", "source_cursors",
            "extraction_runs", "documents", "field_schemas", "connections",
        ):
            c.execute(
                text(f"DELETE FROM {table} WHERE tenant_id IN (:a, :b)"), {"a": TENANT, "b": OTHER}
            )


def _insert_connection(owner, tenant: str = TENANT, cid: str = _CON, status: str = "tested") -> None:
    with owner.begin() as c:
        c.execute(
            text(
                "INSERT INTO connections (id, tenant_id, type, name, config, status)"
                " VALUES (:i, :t, 'webhook', 'hook', '{}'::jsonb, :s)"
            ),
            {"i": cid, "t": tenant, "s": status},
        )


def _insert_workflow(owner, *, tenant: str = TENANT, status: str = "draft", graph=None) -> str:
    wid = f"workflow_{uuid.uuid4().hex[:12]}"
    with owner.begin() as c:
        c.execute(
            text(
                "INSERT INTO workflows (id, tenant_id, name, status, version, graph_json)"
                " VALUES (:i, :t, 'wf', :s, 1, CAST(:g AS jsonb))"
            ),
            {"i": wid, "t": tenant, "s": status, "g": json.dumps(graph or _graph())},
        )
    return wid


def _insert_run(owner, workflow_id: str, *, tenant: str = TENANT, graph=None, status="succeeded") -> str:
    rid = f"wfrun_{uuid.uuid4().hex[:12]}"
    with owner.begin() as c:
        c.execute(
            text(
                "INSERT INTO workflow_runs (id, tenant_id, workflow_id, workflow_version,"
                " trigger, status)"
                " VALUES (:i, :t, :w, 1, CAST(:tr AS jsonb), :s)"
            ),
            {
                "i": rid, "t": tenant, "w": workflow_id, "s": status,
                "tr": json.dumps({"type": "manual", "graph_json": graph or _graph()}),
            },
        )
    return rid


# ---------- 接続 ----------


def test_参照の無い接続は削除されsource_cursorsも一緒に消える(repos, owner) -> None:
    admin, _ = repos
    _insert_connection(owner)
    with owner.begin() as c:
        c.execute(
            text(
                "INSERT INTO source_cursors (id, tenant_id, connection_id, source_key, content_hash)"
                " VALUES (:i, :t, :c, 'k', 'h')"
            ),
            {"i": f"cur_{uuid.uuid4().hex[:8]}", "t": TENANT, "c": _CON},
        )
    assert admin.delete_connection(TENANT, _CON) == {"cursors_deleted": 1}
    assert admin.get_connection(TENANT, _CON) is None
    with owner.begin() as c:
        n = c.execute(
            text("SELECT count(*) FROM source_cursors WHERE tenant_id=:t AND connection_id=:c"),
            {"t": TENANT, "c": _CON},
        ).scalar_one()
    assert n == 0
    # 2 回目は None（無い）
    assert admin.delete_connection(TENANT, _CON) is None


def test_定義が参照する接続はDELETE文のガードで消えない(repos, owner) -> None:
    admin, wf = repos
    _insert_connection(owner)
    wid = _insert_workflow(owner, status="draft")

    refs = wf.workflows_referencing_connection(TENANT, _CON)
    assert [w.id for w in refs] == [wid]
    # status で絞れる（無効化のガードは active だけを見る）
    assert wf.workflows_referencing_connection(TENANT, _CON, statuses=["active"]) == []
    with owner.begin() as c:
        c.execute(text("UPDATE workflows SET status='active' WHERE id=:i"), {"i": wid})
    assert [w.id for w in wf.workflows_referencing_connection(TENANT, _CON, statuses=["active"])] == [wid]
    # 別の接続 id は引っかからない
    assert wf.workflows_referencing_connection(TENANT, "con_nope") == []

    # ルータの事前チェックを飛ばして直接消そうとしても、DELETE 文の NOT EXISTS が守る
    assert admin.delete_connection(TENANT, _CON) is None
    assert admin.get_connection(TENANT, _CON) is not None


def test_runのスナップショットだけが参照する接続も消えない(repos, owner) -> None:
    admin, wf = repos
    _insert_connection(owner)
    # 定義側はもう参照していない（別の接続に差し替えた後）が、run のスナップショットに残る
    wid = _insert_workflow(owner, graph=_graph(connection_id="con_other"))
    _insert_run(owner, wid, graph=_graph())
    assert wf.workflows_referencing_connection(TENANT, _CON) == []
    assert wf.runs_referencing_connection(TENANT, _CON) == 1
    assert admin.delete_connection(TENANT, _CON) is None
    assert admin.get_connection(TENANT, _CON) is not None


def test_他テナントの参照は見えず他テナントの接続は消せない(repos, owner) -> None:
    admin, wf = repos
    _insert_connection(owner)
    # 他テナントが同じ id 文字列を参照していても、自テナントの判定には影響しない
    _insert_workflow(owner, tenant=OTHER)
    assert wf.workflows_referencing_connection(TENANT, _CON) == []
    assert wf.runs_referencing_connection(TENANT, _CON) == 0
    # 他テナントからは消せない
    assert admin.delete_connection(OTHER, _CON) is None
    assert admin.get_connection(TENANT, _CON) is not None


def test_壊れたgraph_jsonがあっても参照検索は落ちない(repos, owner) -> None:
    _, wf = repos
    _insert_connection(owner)
    _insert_workflow(owner, graph={"version": 1, "nodes": "broken", "edges": []})
    assert wf.workflows_referencing_connection(TENANT, _CON) == []


def test_audit_logsのtarget_typeを指定できる(repos, owner) -> None:
    _, wf = repos
    wf.record_audit(
        TENANT, actor_id="sato", action="connection.delete", target_id=_CON,
        target_type="connection", detail={"type": "webhook"},
    )
    with owner.begin() as c:
        row = c.execute(
            text("SELECT target_type, action FROM audit_logs WHERE tenant_id=:t AND target_id=:i"),
            {"t": TENANT, "i": _CON},
        ).first()
    assert row is not None and tuple(row) == ("connection", "connection.delete")
