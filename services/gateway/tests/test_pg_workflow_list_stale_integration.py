"""ワークフロー一覧の旧版スキーマ参照を実 PostgreSQL で検証する（設計 region-template-editor
§4.4b / §11-9）。

InMemory だと検出できないもの:
- ``PgAdminRepository.schema_versions`` の SQL（LATERAL で引く最新版、``ANY(:ids)``、tenant 分離）
- lint L012 の ``PgWorkflowsRepository.schema_is_latest`` と判定が一致すること
  （最新版の定義 ``_LATEST_SCHEMA_LATERAL`` を共有している）
- ``GET /v1/workflows`` の SQL 文の数がワークフロー数に比例しないこと（N+1 にしない）

DATABASE_URL_TEST が設定されている時だけ動く（CI/ローカルは compose の postgres）。
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import pytest

pytest.importorskip("sqlalchemy", reason="Pg リポジトリは runtime 依存")
pytest.importorskip("psycopg", reason="Pg リポジトリは runtime 依存")

from fastapi.testclient import TestClient  # noqa: E402
from gw_helpers import TEST_SECRET, FakeRasterizer, make_token  # noqa: E402
from sqlalchemy import create_engine, event, text  # noqa: E402

_DSN = os.environ.get("DATABASE_URL_TEST")
pytestmark = pytest.mark.skipif(not _DSN, reason="DATABASE_URL_TEST 未設定（実 DB が要る）")


@dataclass(frozen=True)
class Env:
    """1 テストぶんの tenant・版 id と、それに向けた Pg リポジトリ。"""

    admin: Any
    wf: Any
    #: 判定の対象 tenant と、分離の確認に使う他 tenant
    tenant: str
    other: str
    #: invoice は v1〜v3（最新 v3）、receipt は v1 のみ（それが最新）
    inv_v1: str
    inv_v2: str
    inv_v3: str
    rc_v1: str


@pytest.fixture(scope="module")
def owner() -> Iterator[Any]:
    engine = create_engine(_DSN, future=True)  # type: ignore[arg-type]
    yield engine
    engine.dispose()


@pytest.fixture
def env(owner: Any) -> Iterator[Env]:
    """tenant を**テストごとに作って消す**。

    field_schemas は ``UNIQUE (tenant_id, doc_type, version)`` なので、tenant を固定すると
    版 id を一意にしても (invoice, 1) などの (doc_type, version) が衝突する ── 中断した実行が
    残した行や、同じ DB に向けた同時実行と UniqueViolation になる。tenant ごと一意にすれば
    doc_type / version は固定のままでよく、後始末は tenant 単位で消せば残らない
    （field_schemas.id は全テナントで一意の主キーなので、版 id にも同じ接尾辞を付ける）。
    """
    from newfan_gateway.db import PgAdminRepository, PgWorkflowsRepository

    sfx = uuid.uuid4().hex[:12]
    tenant, other = f"ten_wfst_{sfx}", f"ten_wfst_other_{sfx}"
    inv_v1, inv_v2, inv_v3 = (f"sch_st_inv{v}_{sfx}" for v in (1, 2, 3))
    rc_v1 = f"sch_st_rc1_{sfx}"
    with owner.begin() as c:
        for t in (tenant, other):
            c.execute(text("INSERT INTO tenants (id, name) VALUES (:i, 'wf stale test')"), {"i": t})
        for sid, doc_type, version in (
            (inv_v1, "invoice", 1),
            (inv_v2, "invoice", 2),
            (inv_v3, "invoice", 3),
            (rc_v1, "receipt", 1),
        ):
            c.execute(
                text(
                    "INSERT INTO field_schemas (id, tenant_id, doc_type, version, fields)"
                    " VALUES (:i, :t, :d, :v, '[]'::jsonb)"
                ),
                {"i": sid, "t": tenant, "d": doc_type, "v": version},
            )
    admin, wf = PgAdminRepository(_DSN), PgWorkflowsRepository(_DSN)  # type: ignore[arg-type]
    try:
        yield Env(
            admin=admin,
            wf=wf,
            tenant=tenant,
            other=other,
            inv_v1=inv_v1,
            inv_v2=inv_v2,
            inv_v3=inv_v3,
            rc_v1=rc_v1,
        )
    finally:
        admin._engine.dispose()
        wf._engine.dispose()
        with owner.begin() as c:
            # tenants を参照する行を先に消す（field_schemas は tenants への FK を持つ）
            for table in ("audit_logs", "workflows", "field_schemas"):
                c.execute(
                    text(f"DELETE FROM {table} WHERE tenant_id IN (:a, :b)"),
                    {"a": tenant, "b": other},
                )
            c.execute(text("DELETE FROM tenants WHERE id IN (:a, :b)"), {"a": tenant, "b": other})


def _graph(*extracts: tuple[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "version": 1,
        "nodes": [
            {"id": "t1", "type": "source.manual", "config": {}},
            *({"id": nid, "type": "process.extract", "config": cfg} for nid, cfg in extracts),
        ],
        "edges": [{"from": "t1", "to": nid} for nid, _ in extracts],
    }


def _create(env: Env, name: str, graph: dict[str, Any]) -> str:
    from newfan_gateway.records import WorkflowRecord

    rec = env.wf.create_workflow(
        WorkflowRecord(
            id=f"workflow_{uuid.uuid4().hex[:12]}",
            tenant_id=env.tenant,
            name=name,
            graph_json=graph,
            created_by="tester",
        )
    )
    return str(rec.id)


def test_schema_versionsは版と最新版を1回で返しL012の判定と一致する(env: Env) -> None:
    admin, wf, t = env.admin, env.wf, env.tenant
    got = admin.schema_versions(t, [env.inv_v1, env.inv_v3, env.rc_v1, "sch_nope", env.inv_v1])

    assert set(got) == {env.inv_v1, env.inv_v3, env.rc_v1}  # 存在しない id は載らない・重複は畳む
    assert (got[env.inv_v1].doc_type, got[env.inv_v1].version) == ("invoice", 1)
    assert (got[env.inv_v1].latest_schema_id, got[env.inv_v1].latest_version) == (env.inv_v3, 3)
    assert got[env.inv_v3].is_latest and got[env.rc_v1].is_latest
    assert not got[env.inv_v1].is_latest

    # lint L012（schema_is_latest）と同じ判定になる（最新版の定義を共有している）
    for sid in (env.inv_v1, env.inv_v2, env.inv_v3, env.rc_v1):
        expected = admin.schema_versions(t, [sid])[sid].is_latest
        assert wf.schema_is_latest(t, sid) is expected, sid
    assert wf.schema_is_latest(t, "sch_nope") is True  # 不在は L009 の担当

    assert admin.schema_versions(env.other, [env.inv_v1]) == {}  # 他テナントの版は引けない
    assert admin.schema_versions(t, []) == {}


def test_一覧の旧版バッジは実Pgで判定されSQL文数がワークフロー数によらない(
    env: Env, tmp_path: Path
) -> None:
    from newfan_ingest import IngestService
    from newfan_ingest.storage import LocalObjectStore

    from newfan_gateway.app import create_app
    from newfan_gateway.config import Settings

    admin, wf = env.admin, env.wf
    app = create_app(
        settings=Settings(jwt_secret=TEST_SECRET, storage_root=tmp_path),
        admin=admin,
        workflows=wf,
        ingestor=IngestService(LocalObjectStore(tmp_path), FakeRasterizer()),
    )
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {make_token('admin', tenant=env.tenant)}"}

    statements: list[str] = []

    def count(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        statements.append(statement)

    engines = [admin._engine, wf._engine]
    for e in engines:
        event.listen(e, "before_cursor_execute", count)
    try:
        w_old = _create(env, "v1 固定", _graph(("x1", {"schema_id": env.inv_v1})))
        statements.clear()
        r = client.get("/v1/workflows", headers=headers)
        assert r.status_code == 200, r.text
        n_one = len(statements)
        items = {w["id"]: w for w in r.json()["items"]}
        assert items[w_old]["stale_schema_refs"] == [
            {
                "node_id": "x1",
                "doc_type": "invoice",
                "schema_id": env.inv_v1,
                "schema_version": 1,
                "latest_schema_id": env.inv_v3,
                "latest_version": 3,
            }
        ]

        w_mixed = _create(
            env,
            "混在",
            _graph(
                ("x_old", {"schema_id": env.inv_v2}),
                ("x_new", {"schema_id": env.inv_v3}),
                ("x_rc", {"schema_id": env.rc_v1}),
            ),
        )
        w_latest = _create(env, "最新", _graph(("x1", {"schema_id": env.inv_v3})))
        w_dt = _create(env, "種別指定", _graph(("x1", {"doc_type": "invoice"})))
        w_missing = _create(env, "不在", _graph(("x1", {"schema_id": "sch_nope"})))
        statements.clear()
        r = client.get("/v1/workflows", headers=headers)
        assert r.status_code == 200, r.text
        n_many = len(statements)
    finally:
        for e in engines:
            event.remove(e, "before_cursor_execute", count)

    # 1 件でも 5 件でも同じ文数（一覧 1 回 + 版の解決 1 回。各々 RLS の set_config を含む）
    assert n_many == n_one, (n_one, n_many)
    assert sum("field_schemas" in s for s in statements) == 1, statements

    items = {w["id"]: w for w in r.json()["items"]}
    assert len(items) == 5
    assert [(s["node_id"], s["schema_version"], s["latest_version"])
            for s in items[w_mixed]["stale_schema_refs"]] == [("x_old", 2, 3)]
    for wid in (w_latest, w_dt, w_missing):
        assert items[wid]["stale_schema_refs"] == [], items[wid]
