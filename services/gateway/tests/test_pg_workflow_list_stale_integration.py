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

# 他の Pg テスト（test_pg_workflows_integration の ten_wf_test 等）と tenant を分ける
TENANT = "ten_wf_stale_test"
OTHER = "ten_wf_stale_other"

# テストごとに一意な id（並行実行・前回の残骸と衝突させない）
_SFX = uuid.uuid4().hex[:8]
INV_V1 = f"sch_st_inv1_{_SFX}"
INV_V2 = f"sch_st_inv2_{_SFX}"
INV_V3 = f"sch_st_inv3_{_SFX}"
RC_V1 = f"sch_st_rc1_{_SFX}"


@pytest.fixture(scope="module")
def owner() -> Any:
    return create_engine(_DSN, future=True)  # type: ignore[arg-type]


@pytest.fixture
def repos(owner: Any) -> Iterator[tuple[Any, Any]]:
    from newfan_gateway.db import PgAdminRepository, PgWorkflowsRepository

    with owner.begin() as c:
        for t in (TENANT, OTHER):
            c.execute(
                text("INSERT INTO tenants (id, name) VALUES (:i,'wf stale') ON CONFLICT DO NOTHING"),
                {"i": t},
            )
        # invoice は v1〜v3（最新 v3）、receipt は v1 のみ（それが最新）
        for sid, doc_type, version in (
            (INV_V1, "invoice", 1),
            (INV_V2, "invoice", 2),
            (INV_V3, "invoice", 3),
            (RC_V1, "receipt", 1),
        ):
            c.execute(
                text(
                    "INSERT INTO field_schemas (id, tenant_id, doc_type, version, fields)"
                    " VALUES (:i, :t, :d, :v, '[]'::jsonb)"
                ),
                {"i": sid, "t": TENANT, "d": doc_type, "v": version},
            )
    admin, wf = PgAdminRepository(_DSN), PgWorkflowsRepository(_DSN)  # type: ignore[arg-type]
    yield admin, wf
    with owner.begin() as c:
        for table in ("audit_logs", "workflows", "field_schemas"):
            c.execute(
                text(f"DELETE FROM {table} WHERE tenant_id IN (:a, :b)"), {"a": TENANT, "b": OTHER}
            )


def _graph(*extracts: tuple[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "version": 1,
        "nodes": [
            {"id": "t1", "type": "source.manual", "config": {}},
            *({"id": nid, "type": "process.extract", "config": cfg} for nid, cfg in extracts),
        ],
        "edges": [{"from": "t1", "to": nid} for nid, _ in extracts],
    }


def _create(wf: Any, name: str, graph: dict[str, Any]) -> str:
    from newfan_gateway.records import WorkflowRecord

    rec = wf.create_workflow(
        WorkflowRecord(
            id=f"workflow_{uuid.uuid4().hex[:12]}",
            tenant_id=TENANT,
            name=name,
            graph_json=graph,
            created_by="tester",
        )
    )
    return str(rec.id)


def test_schema_versionsは版と最新版を1回で返しL012の判定と一致する(repos: tuple[Any, Any]) -> None:
    admin, wf = repos
    got = admin.schema_versions(TENANT, [INV_V1, INV_V3, RC_V1, "sch_nope", INV_V1])

    assert set(got) == {INV_V1, INV_V3, RC_V1}  # 存在しない id は載らない・重複は畳む
    assert (got[INV_V1].doc_type, got[INV_V1].version) == ("invoice", 1)
    assert (got[INV_V1].latest_schema_id, got[INV_V1].latest_version) == (INV_V3, 3)
    assert got[INV_V3].is_latest and got[RC_V1].is_latest
    assert not got[INV_V1].is_latest

    # lint L012（schema_is_latest）と同じ判定になる（最新版の定義を共有している）
    for sid in (INV_V1, INV_V2, INV_V3, RC_V1):
        expected = admin.schema_versions(TENANT, [sid])[sid].is_latest
        assert wf.schema_is_latest(TENANT, sid) is expected, sid
    assert wf.schema_is_latest(TENANT, "sch_nope") is True  # 不在は L009 の担当

    assert admin.schema_versions(OTHER, [INV_V1]) == {}  # 他テナントの版は引けない
    assert admin.schema_versions(TENANT, []) == {}


def test_一覧の旧版バッジは実Pgで判定されSQL文数がワークフロー数によらない(
    repos: tuple[Any, Any], tmp_path: Path
) -> None:
    from newfan_ingest import IngestService
    from newfan_ingest.storage import LocalObjectStore

    from newfan_gateway.app import create_app
    from newfan_gateway.config import Settings

    admin, wf = repos
    app = create_app(
        settings=Settings(jwt_secret=TEST_SECRET, storage_root=tmp_path),
        admin=admin,
        workflows=wf,
        ingestor=IngestService(LocalObjectStore(tmp_path), FakeRasterizer()),
    )
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {make_token('admin', tenant=TENANT)}"}

    statements: list[str] = []

    def count(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        statements.append(statement)

    engines = [admin._engine, wf._engine]
    for e in engines:
        event.listen(e, "before_cursor_execute", count)
    try:
        w_old = _create(wf, "v1 固定", _graph(("x1", {"schema_id": INV_V1})))
        statements.clear()
        r = client.get("/v1/workflows", headers=headers)
        assert r.status_code == 200, r.text
        n_one = len(statements)
        items = {w["id"]: w for w in r.json()["items"]}
        assert items[w_old]["stale_schema_refs"] == [
            {
                "node_id": "x1",
                "doc_type": "invoice",
                "schema_id": INV_V1,
                "schema_version": 1,
                "latest_schema_id": INV_V3,
                "latest_version": 3,
            }
        ]

        w_mixed = _create(
            wf,
            "混在",
            _graph(
                ("x_old", {"schema_id": INV_V2}),
                ("x_new", {"schema_id": INV_V3}),
                ("x_rc", {"schema_id": RC_V1}),
            ),
        )
        w_latest = _create(wf, "最新", _graph(("x1", {"schema_id": INV_V3})))
        w_dt = _create(wf, "種別指定", _graph(("x1", {"doc_type": "invoice"})))
        w_missing = _create(wf, "不在", _graph(("x1", {"schema_id": "sch_nope"})))
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
