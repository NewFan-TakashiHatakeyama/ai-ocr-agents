"""ワークフロー管理 API（§16 設計 v0.2 §11 / P2）。

契約の要点をテストで固定する:
- 保存はモデル検証のみ（lint error があっても draft 保存できる）
- activate だけが lint error ゼロ + 実装済みノードのみを要求する
- PUT は常に新版（version+1）で draft に戻る（§11.1 有効化＝版の固定）
"""

from __future__ import annotations

import copy
from typing import Any

import jwt
import pytest
from fastapi.testclient import TestClient

from newfan_gateway.app import create_app
from newfan_gateway.config import Settings
from newfan_gateway.workflows_repo import InMemoryWorkflowsRepository

SECRET = "test-secret-0123456789-abcdefghijklmnop"


def _auth(role: str = "admin", tenant: str = "ten_1") -> dict[str, str]:
    tok = jwt.encode({"sub": "u1", "tenant_id": tenant, "role": role}, SECRET, algorithm="HS256")
    return {"Authorization": f"Bearer {tok}"}


# P3 実装範囲のノードだけで組んだ、lint 指摘ゼロで activate できるグラフ
GRAPH_OK: dict[str, Any] = {
    "version": 1,
    "nodes": [
        {"id": "t1", "type": "source.manual", "config": {}},
        {"id": "x1", "type": "process.extract", "config": {"schema_id": "sch_inv"}},
        {
            "id": "b1",
            "type": "branch.condition",
            "config": {
                "branches": [{"when": "run.status == 'needs_review'", "to": "s1"}],
                "else": "m1",
            },
        },
        {
            "id": "m1",
            "type": "transform.map_fields",
            "config": {"mappings": [{"from": "total_amount", "to": "amount"}]},
        },
        {"id": "s1", "type": "sink.webhook", "config": {"connection_id": "con_hook"}},
    ],
    "edges": [
        {"from": "t1", "to": "x1"},
        {"from": "x1", "to": "b1"},
        {"from": "m1", "to": "s1"},
    ],
}


@pytest.fixture
def repo() -> InMemoryWorkflowsRepository:
    r = InMemoryWorkflowsRepository()
    r.seed_schema_id("ten_1", "sch_inv")
    r.seed_connection("ten_1", "con_hook")
    return r


@pytest.fixture
def client(repo: InMemoryWorkflowsRepository) -> TestClient:
    return TestClient(create_app(settings=Settings(jwt_secret=SECRET), workflows=repo))


def _create(client: TestClient, graph: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
    r = client.post(
        "/v1/workflows",
        json={"name": kw.get("name", "請求書自動化"), "graph_json": graph or GRAPH_OK,
              "auto_confirm": kw.get("auto_confirm", False)},
        headers=_auth(),
    )
    assert r.status_code == 201, r.text
    return r.json()  # type: ignore[no-any-return]


# ---------- CRUD と版管理 ----------


def test_作成はdraftのversion1(client: TestClient, repo: InMemoryWorkflowsRepository) -> None:
    w = _create(client)
    assert (w["status"], w["version"]) == ("draft", 1)
    assert w["graph_json"]["nodes"][0]["id"] == "t1"
    assert any(a["action"] == "workflow.create" for a in repo.audits)


def test_不正なgraphは保存時に422(client: TestClient) -> None:
    bad = copy.deepcopy(GRAPH_OK)
    bad["nodes"][0]["type"] = "source.ftp"  # 未知の種別
    r = client.post(
        "/v1/workflows", json={"name": "x", "graph_json": bad}, headers=_auth()
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "E4001"


def test_lintエラーがあってもdraft保存はできる(client: TestClient) -> None:
    # 保存を止めると「編集途中の下書き」が置けない。止めるのは activate だけ（§11）。
    dangling = copy.deepcopy(GRAPH_OK)
    dangling["edges"].append({"from": "s1", "to": "ghost"})  # L006 error
    w = _create(client, graph=dangling)
    assert w["status"] == "draft"


def test_更新は新版になりdraftへ戻る(client: TestClient) -> None:
    w = _create(client)
    client.post(f"/v1/workflows/{w['id']}/activate", headers=_auth())
    r = client.put(
        f"/v1/workflows/{w['id']}",
        json={"graph_json": GRAPH_OK, "name": "改名"},
        headers=_auth(),
    )
    assert r.status_code == 200
    body = r.json()
    # active のまま定義が差し替わると「有効化＝版の固定」が破れる。必ず draft へ。
    assert (body["version"], body["status"], body["name"]) == (2, "draft", "改名")


def test_一覧はgraphを含まず新しい順(client: TestClient) -> None:
    _create(client, name="w1")
    _create(client, name="w2")
    r = client.get("/v1/workflows", headers=_auth())
    items = r.json()["items"]
    assert len(items) == 2
    assert "graph_json" not in items[0]


def test_他テナントからは見えない(client: TestClient) -> None:
    w = _create(client)
    r = client.get(f"/v1/workflows/{w['id']}", headers=_auth(tenant="ten_2"))
    assert r.status_code == 400  # E1001（見つからない）


def test_admin以外は触れない(client: TestClient) -> None:
    r = client.get("/v1/workflows", headers=_auth(role="reviewer"))
    assert r.status_code == 403


# ---------- activate / pause ----------


def test_activateは条件を満たせばactiveになる(
    client: TestClient, repo: InMemoryWorkflowsRepository
) -> None:
    w = _create(client)
    r = client.post(f"/v1/workflows/{w['id']}/activate", headers=_auth())
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "active"
    assert any(a["action"] == "workflow.activate" for a in repo.audits)


def test_lintエラーがあるとactivateできない(client: TestClient) -> None:
    dangling = copy.deepcopy(GRAPH_OK)
    dangling["edges"].append({"from": "s1", "to": "ghost"})
    w = _create(client, graph=dangling)
    r = client.post(f"/v1/workflows/{w['id']}/activate", headers=_auth())
    assert r.status_code == 422
    body = r.json()["error"]
    assert body["code"] == "E4001"
    assert any(f["rule"] == "L006" for f in body["details"]["findings"])


def test_スキーマ未登録だとactivateできない(client: TestClient) -> None:
    # L009: sch_inv を seed していないテナントの図と同じ状況を作る
    other = TestClient(
        create_app(settings=Settings(jwt_secret=SECRET), workflows=InMemoryWorkflowsRepository())
    )
    w = _create(other)
    r = other.post(f"/v1/workflows/{w['id']}/activate", headers=_auth())
    assert r.status_code == 422
    rules = {f["rule"] for f in r.json()["error"]["details"]["findings"]}
    assert "L009" in rules and "L010" in rules


def test_未実装ノードを含むとactivateできない(client: TestClient) -> None:
    # 保存はできる（13 種すべて正当な graph_json）。実行できないから有効化で断る（§4.2）。
    g = copy.deepcopy(GRAPH_OK)
    g["nodes"][0] = {
        "id": "t1",
        "type": "source.schedule",
        "config": {"cron": "0 9 * * 1"},
    }
    w = _create(client, graph=g)
    r = client.post(f"/v1/workflows/{w['id']}/activate", headers=_auth())
    assert r.status_code == 422
    assert r.json()["error"]["details"]["unsupported_types"] == ["source.schedule"]


def test_pauseはactiveのみ(client: TestClient) -> None:
    w = _create(client)
    r = client.post(f"/v1/workflows/{w['id']}/pause", headers=_auth())
    assert r.status_code == 409  # draft は止められない（E1005）
    client.post(f"/v1/workflows/{w['id']}/activate", headers=_auth())
    r = client.post(f"/v1/workflows/{w['id']}/pause", headers=_auth())
    assert r.status_code == 200
    assert r.json()["status"] == "paused"


def test_pause後に再activateできる(client: TestClient) -> None:
    w = _create(client)
    client.post(f"/v1/workflows/{w['id']}/activate", headers=_auth())
    client.post(f"/v1/workflows/{w['id']}/pause", headers=_auth())
    r = client.post(f"/v1/workflows/{w['id']}/activate", headers=_auth())
    assert r.status_code == 200
    assert r.json()["status"] == "active"


# ---------- lint / catalog ----------


def test_lintは保存済みグラフを検査する(client: TestClient) -> None:
    w = _create(client)
    r = client.post(f"/v1/workflows/{w['id']}/lint", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["findings"] == []
    assert body["activatable"] is True


def test_lintは編集中グラフを保存せず検査できる(client: TestClient) -> None:
    w = _create(client)
    dangling = copy.deepcopy(GRAPH_OK)
    dangling["edges"].append({"from": "s1", "to": "ghost"})
    r = client.post(
        f"/v1/workflows/{w['id']}/lint", json={"graph_json": dangling}, headers=_auth()
    )
    assert r.json()["activatable"] is False
    assert any(f["rule"] == "L006" for f in r.json()["findings"])
    # 保存されていない（保存済みは指摘ゼロのまま）
    r2 = client.post(f"/v1/workflows/{w['id']}/lint", headers=_auth())
    assert r2.json()["findings"] == []


def test_catalogは13種のJSONSchemaと実装済み一覧を返す(client: TestClient) -> None:
    r = client.get("/v1/workflows/catalog", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert len(body["types"]) == 13
    assert "schema_id" in body["types"]["process.extract"]["required"]
    assert "source.manual" in body["implemented"]
    assert "source.s3_event" in body["implemented"]  # P4 で実装済み
    assert "branch.hitl_gate" in body["implemented"]  # P5 で実装済み
    assert "source.schedule" not in body["implemented"]  # P8
