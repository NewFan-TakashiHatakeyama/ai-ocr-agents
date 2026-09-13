"""extract ノードの doc_type 指定（実行時に最新版へ解決。設計 region-template-editor §4.4b v2 (b)）。

- 種別が存在すれば有効化できる（L013 なし）。存在しない・アーカイブ済みは L013（error）で止まる
- doc_type 指定は旧版参照（stale-workflows / L012）の対象外
- 種別をアーカイブしようとすると、doc_type 指定の有効ワークフローも参照として拒む
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from gw_helpers import auth

from newfan_gateway.records import SchemaFieldDef, SchemaRecord


def _graph(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": 1,
        "nodes": [
            {"id": "t1", "type": "source.manual", "config": {}},
            {"id": "x1", "type": "process.extract", "config": config},
        ],
        "edges": [{"from": "t1", "to": "x1"}],
    }


def _create(ctx: SimpleNamespace, config: dict[str, Any]) -> str:
    r = ctx.client.post(
        "/v1/workflows", json={"name": "wf", "graph_json": _graph(config)}, headers=auth("admin")
    )
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


def _lint(ctx: SimpleNamespace, wid: str) -> dict[str, Any]:
    r = ctx.client.post(f"/v1/workflows/{wid}/lint", headers=auth("admin"))
    assert r.status_code == 200, r.text
    return r.json()


def test_doc_type指定は種別が存在すれば有効化できる(ctx: SimpleNamespace) -> None:
    wid = _create(ctx, {"doc_type": "invoice"})  # ctx は invoice v4 を seed 済み
    body = _lint(ctx, wid)
    assert not [f for f in body["findings"] if f["rule"] in ("L009", "L012", "L013")], body
    r = ctx.client.post(f"/v1/workflows/{wid}/activate", headers=auth("admin"))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "active"


def test_doc_type指定の未登録種別はL013で止まる(ctx: SimpleNamespace) -> None:
    wid = _create(ctx, {"doc_type": "nope"})
    body = _lint(ctx, wid)
    l013 = [f for f in body["findings"] if f["rule"] == "L013"]
    assert l013 and l013[0]["severity"] == "error" and l013[0]["node_id"] == "x1"
    r = ctx.client.post(f"/v1/workflows/{wid}/activate", headers=auth("admin"))
    assert r.status_code == 422, r.text  # 構成 lint の error は E4001（422）
    assert r.json()["error"]["code"] == "E4001"
    assert any(f["rule"] == "L013" for f in r.json()["error"]["details"]["findings"])


def test_doc_type指定はアーカイブ済み種別だとL013(ctx: SimpleNamespace) -> None:
    ctx.admin.seed_schema(
        SchemaRecord(
            id="sch_old", tenant_id="ten_1", doc_type="old_type", version=1,
            fields=[SchemaFieldDef(name="total_amount", type="money_jpy")],
        )
    )
    r = ctx.client.post("/v1/schemas/old_type/archive", headers=auth("admin"))
    assert r.status_code == 200, r.text
    wid = _create(ctx, {"doc_type": "old_type"})
    assert any(f["rule"] == "L013" for f in _lint(ctx, wid)["findings"])


def test_doc_type指定は旧版参照の対象外(ctx: SimpleNamespace) -> None:
    ctx.admin.seed_schema(
        SchemaRecord(
            id="sch_1_v3", tenant_id="ten_1", doc_type="invoice", version=3,
            fields=[SchemaFieldDef(name="total_amount", type="money_jpy")],
        )
    )
    wid = _create(ctx, {"doc_type": "invoice"})
    r = ctx.client.post(f"/v1/workflows/{wid}/activate", headers=auth("admin"))
    assert r.status_code == 200, r.text
    # 常に最新版へ解決するので「旧版を参照している」一覧には出ない
    r = ctx.client.get("/v1/schemas/invoice/stale-workflows", headers=auth("admin"))
    assert r.status_code == 200, r.text
    assert r.json()["items"] == []


def test_doc_type指定の有効ワークフローがあると種別をアーカイブできない(ctx: SimpleNamespace) -> None:
    wid = _create(ctx, {"doc_type": "invoice"})
    r = ctx.client.post(f"/v1/workflows/{wid}/activate", headers=auth("admin"))
    assert r.status_code == 200, r.text
    r = ctx.client.post("/v1/schemas/invoice/archive", headers=auth("admin"))
    assert r.status_code == 409, r.text
    assert r.json()["error"]["details"]["reason"] == "workflow_active"
    assert [w["id"] for w in r.json()["error"]["details"]["workflows"]] == [wid]
