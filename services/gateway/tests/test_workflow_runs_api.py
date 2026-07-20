"""ワークフロー実行 API（§16 設計 v0.2 §11 / P3）。

- 手動実行は active なワークフローだけ。run は graph_json スナップショットで版を固定する
- retry は failed のみ
"""

from __future__ import annotations

from typing import Any

import jwt
import pytest
from fastapi.testclient import TestClient

from newfan_gateway.app import create_app
from newfan_gateway.config import Settings
from newfan_gateway.records import DocumentRecord, WorkflowNodeRunRecord
from newfan_gateway.repository import InMemoryRepository
from newfan_gateway.workflows_repo import InMemoryWorkflowsRepository

SECRET = "test-secret-0123456789-abcdefghijklmnop"


def _auth(role: str = "admin", tenant: str = "ten_1") -> dict[str, str]:
    tok = jwt.encode({"sub": "u1", "tenant_id": tenant, "role": role}, SECRET, algorithm="HS256")
    return {"Authorization": f"Bearer {tok}"}


GRAPH: dict[str, Any] = {
    "version": 1,
    "nodes": [
        {"id": "t1", "type": "source.manual", "config": {}},
        {"id": "x1", "type": "process.extract", "config": {"schema_id": "sch_inv"}},
        {"id": "s1", "type": "sink.webhook", "config": {"connection_id": "con_hook"}},
    ],
    "edges": [{"from": "t1", "to": "x1"}, {"from": "x1", "to": "s1"}],
}


@pytest.fixture
def ctx() -> tuple[TestClient, InMemoryWorkflowsRepository, Any]:
    wf = InMemoryWorkflowsRepository()
    wf.seed_schema_id("ten_1", "sch_inv")
    wf.seed_connection("ten_1", "con_hook")
    repo = InMemoryRepository()
    repo.create_document(
        DocumentRecord(
            id="doc_1", tenant_id="ten_1", storage_uri="s3://x", mime_type="image/png",
            page_count=1, status="uploaded",
        ),
        [],
    )
    app = create_app(settings=Settings(jwt_secret=SECRET), workflows=wf, repo=repo)
    client = TestClient(app)
    r = client.post("/v1/workflows", json={"name": "wf", "graph_json": GRAPH}, headers=_auth())
    wid = r.json()["id"]
    client.post(f"/v1/workflows/{wid}/activate", headers=_auth())
    return client, wf, {"wid": wid, "queue": app.state.queue}


def test_手動実行はstartをenqueueし版を固定する(ctx) -> None:
    client, wf, meta = ctx
    r = client.post(
        f"/v1/workflows/{meta['wid']}/runs", json={"document_id": "doc_1"}, headers=_auth()
    )
    assert r.status_code == 202, r.text
    run_id = r.json()["workflow_run_id"]
    stream, msg = meta["queue"].messages[-1]
    assert stream == "q.workflow"
    assert msg == {"type": "start", "tenant_id": "ten_1", "workflow_run_id": run_id}
    # 版固定: trigger に graph_json スナップショットが入る（§11.1）
    rec = wf.get_run("ten_1", run_id)
    assert rec is not None
    assert rec.trigger["graph_json"]["nodes"][0]["id"] == "t1"
    assert rec.workflow_version == 1


def test_activeでないワークフローは実行できない(ctx) -> None:
    client, _, meta = ctx
    client.post(f"/v1/workflows/{meta['wid']}/pause", headers=_auth())
    r = client.post(
        f"/v1/workflows/{meta['wid']}/runs", json={"document_id": "doc_1"}, headers=_auth()
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "E1005"


def test_存在しないdocumentは400(ctx) -> None:
    client, _, meta = ctx
    r = client.post(
        f"/v1/workflows/{meta['wid']}/runs", json={"document_id": "doc_ghost"}, headers=_auth()
    )
    assert r.status_code == 400


def test_run一覧と詳細(ctx) -> None:
    client, wf, meta = ctx
    run_id = client.post(
        f"/v1/workflows/{meta['wid']}/runs", json={"document_id": "doc_1"}, headers=_auth()
    ).json()["workflow_run_id"]
    # runner の射影を模す
    rec = wf.get_run("ten_1", run_id)
    assert rec is not None
    rec.state = {"waiting": {"kind": "await_extract", "run_id": "run_9"}}
    wf._node_runs[run_id] = [  # noqa: SLF001 - テスト用の直接投入
        WorkflowNodeRunRecord(node_id="x1", node_type="process.extract", status="running", attempt=1)
    ]

    r = client.get(f"/v1/workflows/{meta['wid']}/runs", headers=_auth(role="viewer"))
    assert r.status_code == 200
    assert [i["id"] for i in r.json()["items"]] == [run_id]

    r = client.get(f"/v1/workflow-runs/{run_id}", headers=_auth(role="viewer"))
    body = r.json()
    assert body["waiting"]["kind"] == "await_extract"
    assert body["node_runs"][0]["node_id"] == "x1"
    # graph_json スナップショットは大きいので API に出さない
    assert "trigger" not in body


def test_retryはfailedのみ(ctx) -> None:
    client, wf, meta = ctx
    run_id = client.post(
        f"/v1/workflows/{meta['wid']}/runs", json={"document_id": "doc_1"}, headers=_auth()
    ).json()["workflow_run_id"]
    r = client.post(f"/v1/workflow-runs/{run_id}/retry", headers=_auth())
    assert r.status_code == 409  # running は retry 不可

    rec = wf.get_run("ten_1", run_id)
    assert rec is not None
    rec.status = "failed"
    r = client.post(f"/v1/workflow-runs/{run_id}/retry", headers=_auth())
    assert r.status_code == 202
    stream, msg = meta["queue"].messages[-1]
    assert (stream, msg["type"]) == ("q.workflow", "retry")
    assert any(a["action"] == "workflow.retry" for a in wf.audits)


def test_他テナントのrunは見えない(ctx) -> None:
    client, _, meta = ctx
    run_id = client.post(
        f"/v1/workflows/{meta['wid']}/runs", json={"document_id": "doc_1"}, headers=_auth()
    ).json()["workflow_run_id"]
    r = client.get(f"/v1/workflow-runs/{run_id}", headers=_auth(tenant="ten_2"))
    assert r.status_code == 400  # E1001
