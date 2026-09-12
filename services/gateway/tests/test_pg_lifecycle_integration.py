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


# ---------- スキーマ ----------


def _insert_schema(owner, sid: str, version: int, *, is_active: bool = True, doc_type="invoice") -> None:
    with owner.begin() as c:
        c.execute(
            text(
                "INSERT INTO field_schemas (id, tenant_id, doc_type, version, fields, is_active)"
                " VALUES (:i, :t, :d, :v, '[{\"name\":\"total_amount\",\"type\":\"money_jpy\"}]'::jsonb, :a)"
            ),
            {"i": sid, "t": TENANT, "d": doc_type, "v": version, "a": is_active},
        )


def _is_active_by_id(owner) -> dict[str, bool]:
    with owner.begin() as c:
        rows = c.execute(
            text("SELECT id, is_active FROM field_schemas WHERE tenant_id=:t"), {"t": TENANT}
        ).all()
    return {r[0]: r[1] for r in rows}


def test_アーカイブは全版のis_activeを落とし復元で戻す(repos, owner) -> None:
    admin, _ = repos
    _insert_schema(owner, f"{_SCH}_v1", 1)
    _insert_schema(owner, _SCH, 2)
    _insert_schema(owner, f"{_SCH}_other", 1, doc_type="receipt")

    rec = admin.set_schema_archived(TENANT, "invoice", True)
    assert rec is not None and rec.archived is True and rec.version == 2
    assert _is_active_by_id(owner) == {f"{_SCH}_v1": False, _SCH: False, f"{_SCH}_other": True}
    # 一覧は既定で隠す。include_archived で archived=True 付きで見える
    assert [s.doc_type for s in admin.list_schemas(TENANT)] == ["receipt"]
    shown = admin.list_schemas(TENANT, include_archived=True)
    assert [(s.doc_type, s.version, s.archived) for s in shown] == [
        ("invoice", 2, True), ("receipt", 1, False),
    ]
    # doc_type 直引き・id 直引き（旧版 id も）は archived=True で返る（行は消えていない）
    assert admin.get_schema(TENANT, "invoice").archived is True
    assert admin.get_schema_by_id(TENANT, f"{_SCH}_v1").archived is True
    assert admin.schema_ids_for_doc_type(TENANT, "invoice") == [f"{_SCH}_v1", _SCH]

    rec = admin.set_schema_archived(TENANT, "invoice", False)
    assert rec is not None and rec.archived is False
    assert all(_is_active_by_id(owner).values())
    # 無い doc_type は None
    assert admin.set_schema_archived(TENANT, "nope", True) is None


def test_seed済み環境の旧版だけfalseはアーカイブではない(repos, owner) -> None:
    # scripts/seed_schemas.py は同 doc_type の旧版を is_active=false にしてから seed 版を
    # true で入れる。「最新版の is_active」で判定すると、seed 版より大きい version の
    # 行があるだけで doc_type ごと隠れる。判定は「全版が false」でなければならない
    admin, _ = repos
    _insert_schema(owner, f"{_SCH}_v1", 1, is_active=True)
    _insert_schema(owner, _SCH, 2, is_active=False)
    assert [(s.version, s.archived) for s in admin.list_schemas(TENANT)] == [(2, False)]
    assert admin.get_schema(TENANT, "invoice").archived is False
    assert admin.get_schema_by_id(TENANT, _SCH).archived is False


def test_アーカイブ済みへの新版はput_schemaが同一トランザクションで拒む(repos, owner) -> None:
    from newfan_gateway.admin import SchemaArchivedError
    from newfan_gateway.records import SchemaFieldDef

    admin, _ = repos
    _insert_schema(owner, _SCH, 1)
    admin.set_schema_archived(TENANT, "invoice", True)
    with pytest.raises(SchemaArchivedError):
        admin.put_schema(TENANT, "invoice", [SchemaFieldDef(name="x", type="string")])
    assert admin.schema_ids_for_doc_type(TENANT, "invoice") == [_SCH]  # 増えていない
    # 復元すれば新版を足せる
    admin.set_schema_archived(TENANT, "invoice", False)
    rec = admin.put_schema(TENANT, "invoice", [SchemaFieldDef(name="x", type="string")])
    assert rec.version == 2 and rec.archived is False


def test_旧版idを固定保持するワークフローもスキーマ参照として引ける(repos, owner) -> None:
    admin, wf = repos
    _insert_schema(owner, f"{_SCH}_v1", 1)
    _insert_schema(owner, _SCH, 2)
    wid = _insert_workflow(owner, status="active", graph=_graph(schema_id=f"{_SCH}_v1"))
    ids = admin.schema_ids_for_doc_type(TENANT, "invoice")
    assert [w.id for w in wf.workflows_referencing_schema(TENANT, ids, statuses=["active"])] == [wid]
    assert wf.workflows_referencing_schema(TENANT, [_SCH]) == []  # 最新版だけでは見つからない
    assert wf.workflows_referencing_schema(TENANT, []) == []


def test_アーカイブしてもextraction_runsのFKは保たれ定義を辿れる(repos, owner) -> None:
    admin, _ = repos
    _insert_schema(owner, _SCH, 1)
    doc_id = f"doc_{uuid.uuid4().hex[:12]}"
    run_id = f"run_{uuid.uuid4().hex[:12]}"
    with owner.begin() as c:
        c.execute(
            text(
                "INSERT INTO documents (id, tenant_id, storage_uri, mime_type)"
                " VALUES (:i, :t, 's3://b/k', 'image/png')"
            ),
            {"i": doc_id, "t": TENANT},
        )
        c.execute(
            text(
                "INSERT INTO extraction_runs (id, tenant_id, document_id, schema_id, engine_versions)"
                " VALUES (:i, :t, :d, :s, '{}'::jsonb)"
            ),
            {"i": run_id, "t": TENANT, "d": doc_id, "s": _SCH},
        )
    assert admin.set_schema_archived(TENANT, "invoice", True) is not None
    with owner.begin() as c:
        sid = c.execute(
            text("SELECT schema_id FROM extraction_runs WHERE id=:i"), {"i": run_id}
        ).scalar_one()
    assert sid == _SCH
    # 抽出結果側は id 直引きで定義（項目名）を今までどおり読める
    rec = admin.get_schema_by_id(TENANT, _SCH)
    assert rec is not None and rec.archived is True and rec.fields[0].name == "total_amount"
    # 物理削除なら FK 違反になる（アーカイブが要る理由）
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        with owner.begin() as c:
            c.execute(text("DELETE FROM field_schemas WHERE id=:i"), {"i": _SCH})


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
