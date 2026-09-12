"""旧版を固定保持している有効ワークフローの検出（設計 region-template-editor §4.4b / D17）。

ワークフローの ``process.extract`` は ``schema_id``（版 id）固定なので、テンプレート化や
領域編集で保存した新版は既存ワークフローに自動適用されない。web は保存後に
``GET /v1/schemas/{doc_type}/stale-workflows`` を呼んで警告する。

以前は web が「直前の版の id」だけで graph_json を突合していたため、v1 固定の
ワークフローが v3 保存時に警告から漏れていた（第 3 回敵対的レビュー 2）。ここでは
**全旧版**が対象になること、最新版・draft は含まれないことを固定する。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from gw_helpers import auth

from newfan_gateway.records import SchemaFieldDef, SchemaRecord


def _graph(schema_id: str) -> dict[str, Any]:
    return {
        "version": 1,
        "nodes": [
            {"id": "t1", "type": "source.manual", "config": {}},
            {"id": "x1", "type": "process.extract", "config": {"schema_id": schema_id}},
        ],
        "edges": [{"from": "t1", "to": "x1"}],
    }


def _seed_version(ctx: SimpleNamespace, schema_id: str, version: int) -> None:
    ctx.admin.seed_schema(
        SchemaRecord(
            id=schema_id, tenant_id="ten_1", doc_type="invoice", version=version,
            fields=[SchemaFieldDef(name="total_amount", type="money_jpy")],
        )
    )


def _workflow(ctx: SimpleNamespace, name: str, schema_id: str, *, activate: bool) -> str:
    wf = ctx.client.app.state.workflows
    wf.seed_schema_id("ten_1", schema_id)  # L009（存在確認）を満たす
    r = ctx.client.post(
        "/v1/workflows", json={"name": name, "graph_json": _graph(schema_id)}, headers=auth("admin")
    )
    assert r.status_code == 201, r.text
    wid = str(r.json()["id"])
    if activate:
        r = ctx.client.post(f"/v1/workflows/{wid}/activate", headers=auth("admin"))
        assert r.status_code == 200, r.text
    return wid


def test_全旧版を固定保持する有効ワークフローだけを返す(ctx: SimpleNamespace) -> None:
    # 既定の seed は sch_1 = invoice v4（最新）。v2 / v3 を足す
    _seed_version(ctx, "sch_1_v2", 2)
    _seed_version(ctx, "sch_1_v3", 3)
    w_v2 = _workflow(ctx, "v2 固定", "sch_1_v2", activate=True)  # 直前の版ではない旧版
    w_v3 = _workflow(ctx, "v3 固定", "sch_1_v3", activate=True)  # 直前の版
    _workflow(ctx, "最新", "sch_1", activate=True)  # 最新版 → 対象外
    _workflow(ctx, "下書き", "sch_1_v3", activate=False)  # draft → 対象外（有効化時に L012）

    r = ctx.client.get("/v1/schemas/invoice/stale-workflows", headers=auth("admin"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["doc_type"] == "invoice"
    assert body["latest_schema_id"] == "sch_1" and body["latest_version"] == 4
    got = {(i["id"], i["schema_id"], i["schema_version"], i["status"]) for i in body["items"]}
    assert got == {
        (w_v2, "sch_1_v2", 2, "active"),
        (w_v3, "sch_1_v3", 3, "active"),
    }
    names = {i["name"] for i in body["items"]}
    assert names == {"v2 固定", "v3 固定"}


def test_旧版が無ければ空(ctx: SimpleNamespace) -> None:
    _workflow(ctx, "最新", "sch_1", activate=True)
    r = ctx.client.get("/v1/schemas/invoice/stale-workflows", headers=auth("admin"))
    assert r.status_code == 200, r.text
    assert r.json()["items"] == []
    assert r.json()["latest_schema_id"] == "sch_1"


def test_未知の_doc_type_は_E1001(ctx: SimpleNamespace) -> None:
    r = ctx.client.get("/v1/schemas/nope/stale-workflows", headers=auth("admin"))
    assert r.status_code == 400, r.text  # E1001（不在）は 400
    assert r.json()["error"]["code"] == "E1001"


def test_admin_以外は_403(ctx: SimpleNamespace) -> None:
    r = ctx.client.get("/v1/schemas/invoice/stale-workflows", headers=auth("reviewer"))
    assert r.status_code == 403, r.text
