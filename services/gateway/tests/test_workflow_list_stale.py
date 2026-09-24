"""ワークフロー一覧の旧版スキーマ参照（設計 region-template-editor §4.4b / §11-9）。

``GET /v1/workflows`` の各行に ``stale_schema_refs``（ノード id・doc_type・参照版・最新版）
を載せ、web の一覧が「⚠ 旧版スキーマ」バッジを出す。以前は保存後トースト（stale-workflows）
と lint L012 だけで、一覧からは分からなかった。

固定すること:
- 判定は stale-workflows と同じ（schema_id 指定で当該 doc_type の最新版でない）。
  doc_type 指定・最新版・存在しない id は載らない
- status を問わない（draft も出す）。stale-workflows は active だけ（保存後の警告用）
- 版番号の解決は全行ぶんまとめて 1 回（ワークフロー数に比例した呼び出しにしない）
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from gw_helpers import auth

from newfan_gateway.records import SchemaFieldDef, SchemaRecord


def _extract(node_id: str, config: dict[str, Any]) -> dict[str, Any]:
    return {"id": node_id, "type": "process.extract", "config": config}


def _graph(*extracts: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": 1,
        "nodes": [{"id": "t1", "type": "source.manual", "config": {}}, *extracts],
        "edges": [{"from": "t1", "to": x["id"]} for x in extracts],
    }


def _seed(ctx: SimpleNamespace, schema_id: str, doc_type: str, version: int) -> None:
    ctx.admin.seed_schema(
        SchemaRecord(
            id=schema_id, tenant_id="ten_1", doc_type=doc_type, version=version,
            fields=[SchemaFieldDef(name="total_amount", type="money_jpy")],
        )
    )
    ctx.client.app.state.workflows.seed_schema_id("ten_1", schema_id)  # L009（存在確認）


def _create(ctx: SimpleNamespace, name: str, graph: dict[str, Any], *, activate: bool = False) -> str:
    r = ctx.client.post(
        "/v1/workflows", json={"name": name, "graph_json": graph}, headers=auth("admin")
    )
    assert r.status_code == 201, r.text
    wid = str(r.json()["id"])
    if activate:
        r = ctx.client.post(f"/v1/workflows/{wid}/activate", headers=auth("admin"))
        assert r.status_code == 200, r.text
    return wid


def _list(ctx: SimpleNamespace) -> dict[str, dict[str, Any]]:
    r = ctx.client.get("/v1/workflows", headers=auth("admin"))
    assert r.status_code == 200, r.text
    return {w["id"]: w for w in r.json()["items"]}


def test_旧版を固定保持するノードの内訳を載せる(ctx: SimpleNamespace) -> None:
    # 既定の seed は sch_1 = invoice v4（最新）。v2 を足す
    _seed(ctx, "sch_1_v2", "invoice", 2)
    wid = _create(ctx, "v2 固定", _graph(_extract("x1", {"schema_id": "sch_1_v2"})))

    got = _list(ctx)[wid]["stale_schema_refs"]
    assert got == [
        {
            "node_id": "x1",
            "doc_type": "invoice",
            "schema_id": "sch_1_v2",
            "schema_version": 2,
            "latest_schema_id": "sch_1",
            "latest_version": 4,
        }
    ]


def test_最新版_doc_type指定_存在しないidは載らない(ctx: SimpleNamespace) -> None:
    w_latest = _create(ctx, "最新", _graph(_extract("x1", {"schema_id": "sch_1"})))
    w_doc_type = _create(ctx, "種別指定", _graph(_extract("x1", {"doc_type": "invoice"})))
    # 存在しない版は「最新でない」ではなく「存在しない」（L009 が error で出す）
    w_missing = _create(ctx, "不在", _graph(_extract("x1", {"schema_id": "sch_nope"})))

    items = _list(ctx)
    for wid in (w_latest, w_doc_type, w_missing):
        assert items[wid]["stale_schema_refs"] == [], items[wid]


def test_複数ノードは旧版のものだけを別種別も含めて載せる(ctx: SimpleNamespace) -> None:
    _seed(ctx, "sch_1_v3", "invoice", 3)
    _seed(ctx, "sch_rc_v1", "receipt", 1)
    _seed(ctx, "sch_rc_v2", "receipt", 2)
    wid = _create(
        ctx,
        "混在",
        _graph(
            _extract("x_inv_old", {"schema_id": "sch_1_v3"}),
            _extract("x_inv_new", {"schema_id": "sch_1"}),
            _extract("x_rc_old", {"schema_id": "sch_rc_v1"}),
            _extract("x_rc_dt", {"doc_type": "receipt"}),
        ),
    )

    got = {
        (r["node_id"], r["doc_type"], r["schema_version"], r["latest_version"])
        for r in _list(ctx)[wid]["stale_schema_refs"]
    }
    assert got == {("x_inv_old", "invoice", 3, 4), ("x_rc_old", "receipt", 1, 2)}


def test_status_を問わず載せ_stale_workflowsはactiveだけ(ctx: SimpleNamespace) -> None:
    _seed(ctx, "sch_1_v2", "invoice", 2)
    w_active = _create(ctx, "有効", _graph(_extract("x1", {"schema_id": "sch_1_v2"})), activate=True)
    w_draft = _create(ctx, "下書き", _graph(_extract("x1", {"schema_id": "sch_1_v2"})))

    items = _list(ctx)
    assert items[w_active]["status"] == "active" and items[w_draft]["status"] == "draft"
    for wid in (w_active, w_draft):
        assert [r["schema_id"] for r in items[wid]["stale_schema_refs"]] == ["sch_1_v2"]

    # 同じ判定（stale_schema_refs）を通る stale-workflows と食い違わない（active の範囲で一致）
    r = ctx.client.get("/v1/schemas/invoice/stale-workflows", headers=auth("admin"))
    assert r.status_code == 200, r.text
    from_endpoint = {(i["id"], i["schema_id"]) for i in r.json()["items"]}
    from_list = {
        (w["id"], ref["schema_id"])
        for w in items.values()
        if w["status"] == "active"
        for ref in w["stale_schema_refs"]
        if ref["doc_type"] == "invoice"
    }
    assert from_endpoint == from_list == {(w_active, "sch_1_v2")}


def test_新版を保存すると旧版になる(ctx: SimpleNamespace) -> None:
    wid = _create(ctx, "v4 固定", _graph(_extract("x1", {"schema_id": "sch_1"})))
    assert _list(ctx)[wid]["stale_schema_refs"] == []

    r = ctx.client.put(
        "/v1/schemas",
        json={"doc_type": "invoice", "fields": [{"name": "total_amount", "type": "money_jpy"}]},
        headers=auth("admin"),
    )
    assert r.status_code == 200, r.text
    new_id, new_version = r.json()["id"], r.json()["version"]

    got = _list(ctx)[wid]["stale_schema_refs"]
    assert [(g["schema_id"], g["schema_version"], g["latest_schema_id"], g["latest_version"])
            for g in got] == [("sch_1", 4, new_id, new_version)]


def test_版番号の解決はワークフロー数によらず1回(
    ctx: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(ctx, "sch_1_v2", "invoice", 2)
    for i in range(5):
        _create(ctx, f"wf{i}", _graph(_extract("x1", {"schema_id": "sch_1_v2" if i % 2 else "sch_1"})))

    calls: list[list[str]] = []
    orig = ctx.admin.schema_versions

    def spy(tenant_id: str, schema_ids: Any) -> Any:
        ids = list(schema_ids)
        calls.append(ids)
        return orig(tenant_id, ids)

    monkeypatch.setattr(ctx.admin, "schema_versions", spy)

    # SQL 文の数が行数によらないことは実 Pg で数える（test_pg_workflows_integration.py）
    items = _list(ctx)
    assert len(calls) == 1
    assert sorted(calls[0]) == ["sch_1", "sch_1_v2"]  # 重複は畳んで渡す
    assert sum(1 for w in items.values() if w["stale_schema_refs"]) == 2


def test_単体取得には載せない(ctx: SimpleNamespace) -> None:
    _seed(ctx, "sch_1_v2", "invoice", 2)
    wid = _create(ctx, "v2 固定", _graph(_extract("x1", {"schema_id": "sch_1_v2"})))
    r = ctx.client.get(f"/v1/workflows/{wid}", headers=auth("admin"))
    assert r.status_code == 200, r.text
    assert "stale_schema_refs" not in r.json()
