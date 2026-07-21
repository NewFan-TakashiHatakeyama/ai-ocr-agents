"""接続管理 + dry-run（§16.5 / §9 / P6）。

契約の要点:
- 秘密は受け取らない（config に secret 系キーがあれば 422。secret_ref のみ）
- 疎通テスト成功で status='tested'（sink は tested/active しか使わない）
- dry-run の SQL は実行側（orchestrator）と同一実装で生成される
- db_write を含むワークフローの activate は dry-run 成功が前提
"""

from __future__ import annotations

from typing import Any

import jwt
import pytest
from fastapi.testclient import TestClient

from newfan_gateway.admin import InMemoryAdminRepository
from newfan_gateway.app import create_app
from newfan_gateway.config import Settings
from newfan_gateway.ports import FakeSecretStore
from newfan_gateway.workflows_repo import InMemoryWorkflowsRepository

SECRET = "test-secret-0123456789-abcdefghijklmnop"


def _auth(role: str = "admin") -> dict[str, str]:
    tok = jwt.encode({"sub": "u1", "tenant_id": "ten_1", "role": role}, SECRET, algorithm="HS256")
    return {"Authorization": f"Bearer {tok}"}


GRAPH_DB: dict[str, Any] = {
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
        {
            "id": "d1",
            "type": "sink.db_write",
            "config": {
                "connection_id": "con_erp",
                "table": "erp_demo.invoices",
                "mode": "upsert",
                "keys": ["invoice_no"],
            },
        },
    ],
    "edges": [
        {"from": "t1", "to": "x1"},
        {"from": "x1", "to": "m1"},
        {"from": "m1", "to": "d1"},
    ],
}


@pytest.fixture
def env() -> tuple[TestClient, InMemoryAdminRepository, InMemoryWorkflowsRepository, FakeSecretStore]:
    admin = InMemoryAdminRepository()
    workflows = InMemoryWorkflowsRepository()
    workflows.seed_schema_id("ten_1", "sch_inv")
    secret_store = FakeSecretStore()
    client = TestClient(
        create_app(
            settings=Settings(jwt_secret=SECRET),
            admin=admin,
            workflows=workflows,
            secret_store=secret_store,
        )
    )
    return client, admin, workflows, secret_store


def _create_conn(client: TestClient, **kw: Any) -> dict[str, Any]:
    body = {
        "type": "postgres",
        "name": "顧客基幹DB",
        "config": {"host": "erp.example", "dbname": "erp", "user": "sink"},
        "secret_ref": "arn:fake:ai-ocr/test/conn/ten_1/erp",
        "allowed_tables": ["erp_demo.invoices"],
        **kw,
    }
    r = client.post("/v1/connections", json=body, headers=_auth())
    assert r.status_code == 201, r.text
    return r.json()


# ---------- 接続管理 ----------


def test_接続を登録して一覧できる(env) -> None:
    client, _, _, _ = env
    c = _create_conn(client)
    assert (c["type"], c["status"]) == ("postgres", "untested")
    items = client.get("/v1/connections", headers=_auth()).json()["items"]
    assert [i["id"] for i in items] == [c["id"]]


def test_configに秘密が紛れたら拒否する(env) -> None:
    client, _, _, _ = env
    r = client.post(
        "/v1/connections",
        json={"type": "postgres", "name": "x", "config": {"password": "leak"}},
        headers=_auth(),
    )
    assert r.status_code == 422
    assert "password" in str(r.json()["error"]["details"]["keys"])


def test_未対応typeと不正なallowed_tablesは拒否(env) -> None:
    client, _, _, _ = env
    r = client.post("/v1/connections", json={"type": "ftp", "name": "x"}, headers=_auth())
    assert r.status_code == 422
    r = client.post(
        "/v1/connections",
        json={"type": "postgres", "name": "x", "allowed_tables": ["erp;DROP"]},
        headers=_auth(),
    )
    assert r.status_code == 422


def test_疎通テストはpostgresのみで失敗理由を返す(env) -> None:
    client, _, _, store = env
    c = _create_conn(client)
    # FakeSecretStore に接続不能な DSN を入れる → ok=false（status は untested のまま）
    store.values["arn:fake:ai-ocr/test/conn/ten_1/erp"] = "postgresql://u:p@127.0.0.1:1/nx"
    r = client.post(f"/v1/connections/{c['id']}/test", headers=_auth())
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert client.get("/v1/connections", headers=_auth()).json()["items"][0]["status"] == "untested"

    w = _create_conn(client, type="webhook", config={}, allowed_tables=[])
    r = client.post(f"/v1/connections/{w['id']}/test", headers=_auth())
    assert r.status_code == 409  # E1005: DB のみ対応


# ---------- dry-run ----------


def _create_wf(client: TestClient, graph: dict[str, Any]) -> str:
    r = client.post(
        "/v1/workflows", json={"name": "db書込み", "graph_json": graph}, headers=_auth()
    )
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


def _seed_tested_conn(admin: InMemoryAdminRepository, workflows: InMemoryWorkflowsRepository) -> None:
    rec = admin.create_connection(
        "ten_1", type="postgres", name="erp",
        config={"host": "h", "dbname": "d", "user": "u"},
        secret_ref="arn:fake:ai-ocr/test/conn/ten_1/erp", allowed_tables=["erp_demo.invoices"],
    )
    admin._connections[-1] = rec.model_copy(update={"id": "con_erp", "status": "tested"})
    workflows.seed_connection("ten_1", "con_erp")


def test_dry_runはプレビューSQLを返しそれは実装共有で実SQLに等しい(env) -> None:
    client, admin, workflows, _ = env
    _seed_tested_conn(admin, workflows)
    wf_id = _create_wf(client, GRAPH_DB)
    r = client.post(f"/v1/workflows/{wf_id}/dry-run", headers=_auth())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    sink = body["sinks"][0]
    # 実行側と同じ builder（dbsink.build_db_write_sql）の出力そのもの
    from newfan_workflow.dbsink import build_db_write_sql
    from newfan_workflow.models import DbWriteConfig

    expected = build_db_write_sql(
        DbWriteConfig(
            connection_id="con_erp", table="erp_demo.invoices",
            mode="upsert", keys=["invoice_no"],
        ),
        ["invoice_no", "amount"],
    )
    assert sink["sql"] == expected
    assert sink["columns"] == ["invoice_no", "amount"]


def test_dry_runは疎通未確認とallowed_tables外とマッピング無しを落とす(env) -> None:
    client, admin, workflows, _ = env
    # 1) 接続はあるが untested
    rec = admin.create_connection(
        "ten_1", type="postgres", name="erp", config={},
        secret_ref="arn:fake:ai-ocr/test/conn/ten_1/x", allowed_tables=["erp_demo.invoices"],
    )
    admin._connections[-1] = rec.model_copy(update={"id": "con_erp"})
    workflows.seed_connection("ten_1", "con_erp")
    wf_id = _create_wf(client, GRAPH_DB)
    body = client.post(f"/v1/workflows/{wf_id}/dry-run", headers=_auth()).json()
    assert body["ok"] is False and "疎通未確認" in body["sinks"][0]["error"]

    # 2) tested だが allowed_tables 外
    admin._connections[-1] = admin._connections[-1].model_copy(
        update={"status": "tested", "allowed_tables": ["other.table"]}
    )
    body = client.post(f"/v1/workflows/{wf_id}/dry-run", headers=_auth()).json()
    assert body["ok"] is False and "allowed_tables" in body["sinks"][0]["error"]

    # 3) マッピング無し（列が事前確定できない）
    admin._connections[-1] = admin._connections[-1].model_copy(
        update={"allowed_tables": ["erp_demo.invoices"]}
    )
    g = {**GRAPH_DB, "edges": [
        {"from": "t1", "to": "x1"}, {"from": "x1", "to": "d1"},
    ]}
    g["nodes"] = [n for n in GRAPH_DB["nodes"] if n["id"] != "m1"]
    wf2 = _create_wf(client, g)
    body = client.post(f"/v1/workflows/{wf2}/dry-run", headers=_auth()).json()
    assert body["ok"] is False and "map_fields" in body["sinks"][0]["error"]


def test_activateはdry_run失敗を422で断る(env) -> None:
    client, admin, workflows, _ = env
    # 接続未登録のまま activate（L010 は workflows 側 seed が無いことでも落ちるため、
    # workflows には seed して dry-run だけが落ちる状況を作る）
    workflows.seed_connection("ten_1", "con_erp")
    wf_id = _create_wf(client, GRAPH_DB)
    r = client.post(f"/v1/workflows/{wf_id}/activate", headers=_auth())
    assert r.status_code == 422
    assert "dry_run" in r.json()["error"]["details"]

    # 接続を整えると activate できる
    _seed_tested_conn(admin, workflows)
    r = client.post(f"/v1/workflows/{wf_id}/activate", headers=_auth())
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "active"


# ---------- webhook secret_ref 移行 ----------


def test_webhook登録はsecretをSecretsManagerに置きDBはrefのみ(env) -> None:
    client, admin, _, store = env
    r = client.post(
        "/v1/webhooks/endpoints",
        json={"url": "https://example.com/hook", "name": "hook"},
        headers=_auth(),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["secret"]  # 登録時に一度だけ返る
    rec = admin.list_webhook_endpoints("ten_1")[0]
    assert rec.secret_ref and rec.secret_ref.startswith("arn:fake:")
    assert "secret" not in rec.config  # DB（config）に平文を残さない
    assert store.get(rec.secret_ref) == body["secret"]  # 実体は保管先にある


def test_ネストや近縁キーの秘密も拒否する(env) -> None:
    # トップレベル完全一致だけだと config={"opts": {"password": ...}} や passwd で
    # 平文秘密が DB 保存 + GET 再露出される（レビューで実証・修正）
    client, _, _, _ = env
    for cfg in ({"opts": {"password": "leak"}}, {"passwd": "leak"},
                {"aws": {"secret_access_key": "leak"}}):
        r = client.post(
            "/v1/connections",
            json={"type": "postgres", "name": "x", "config": cfg},
            headers=_auth(),
        )
        assert r.status_code == 422, cfg


def test_他テナント名前空間のsecret_refは拒否する(env) -> None:
    # secret_ref の帰属検証が無いと、他テナントの秘密名を自分の接続に張って
    # 自分のホストへパスワードを送出させられる（クロステナント窃取。レビューで実証・修正）
    client, _, _, _ = env
    r = client.post(
        "/v1/connections",
        json={
            "type": "postgres", "name": "x",
            "secret_ref": "arn:aws:secretsmanager:xx:1:secret:ai-ocr/prod/conn/ten_2/victim",
        },
        headers=_auth(),
    )
    assert r.status_code == 422
    assert "名前空間" in r.json()["error"]["message"]


def test_dry_runはdb_write直前のmapが複数だと拒否する(env) -> None:
    # 分岐で別々の map から合流すると実行される分岐により列が変わり、
    # 「プレビュー = 実 SQL」が破れる（レビューで実証・修正）
    client, admin, workflows, _ = env
    _seed_tested_conn(admin, workflows)
    g = {**GRAPH_DB}
    g["nodes"] = [*GRAPH_DB["nodes"], {
        "id": "m2",
        "type": "transform.map_fields",
        "config": {"mappings": [{"from": "total_amount", "to": "amount"}]},
    }]
    g["edges"] = [*GRAPH_DB["edges"], {"from": "m2", "to": "d1"}]
    wf_id = _create_wf(client, g)
    body = client.post(f"/v1/workflows/{wf_id}/dry-run", headers=_auth()).json()
    assert body["ok"] is False
    assert "1 つにして" in body["sinks"][0]["error"]
