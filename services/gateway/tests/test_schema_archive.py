"""スキーマのアーカイブ / 復元（C9-D）。

契約の要点:
- アーカイブは全版の is_active=false（行は消さない。extraction_runs の FK と過去の
  抽出結果の定義を保つ）。復元で元に戻る
- GET /schemas・/doc-types・分類候補は既定でアーカイブ済みを出さない
  （?include_archived=true でだけ見える）。GET /schemas/{doc_type} は E1001
- PUT /schemas（新規作成・編集の両方）は E1005。chat 経路も ok=False
- 有効なワークフローの extract.schema_id が（どの版でも）指していればアーカイブは E1005
- アーカイブ済みスキーマを指すワークフローは有効化できない（L009）
- アーカイブ済みの schema_id で新しい抽出 run は始められない（/extract・chat rerun_extract）
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from gw_helpers import PDF, auth

from newfan_gateway.records import SchemaFieldDef, SchemaRecord

GRAPH: dict[str, Any] = {
    "version": 1,
    "nodes": [
        {"id": "t1", "type": "source.manual", "config": {}},
        # 旧版（sch_1 v4 ではなく v3）を固定保持しているワークフロー
        {"id": "x1", "type": "process.extract", "config": {"schema_id": "sch_1_v3"}},
    ],
    "edges": [{"from": "t1", "to": "x1"}],
}


def _seed_old_version(ctx: SimpleNamespace) -> None:
    ctx.admin.seed_schema(
        SchemaRecord(
            id="sch_1_v3", tenant_id="ten_1", doc_type="invoice", version=3,
            fields=[SchemaFieldDef(name="total_amount", type="money_jpy")],
        )
    )


def _create_active_workflow(ctx: SimpleNamespace) -> str:
    wf = ctx.client.app.state.workflows
    wf.seed_schema_id("ten_1", "sch_1_v3")
    r = ctx.client.post(
        "/v1/workflows", json={"name": "wf", "graph_json": GRAPH}, headers=auth("admin")
    )
    assert r.status_code == 201, r.text
    wid = str(r.json()["id"])
    r = ctx.client.post(f"/v1/workflows/{wid}/activate", headers=auth("admin"))
    assert r.status_code == 200, r.text
    return wid


def test_アーカイブすると一覧と候補から消え復元で戻る(ctx: SimpleNamespace) -> None:
    _seed_old_version(ctx)
    r = ctx.client.post("/v1/schemas/invoice/archive", headers=auth("admin"))
    assert r.status_code == 200, r.text
    assert r.json()["archived"] is True and r.json()["version"] == 4
    # 全版がアーカイブ状態（旧版 id も）
    assert ctx.admin.get_schema_by_id("ten_1", "sch_1_v3").archived is True
    assert ctx.admin.get_schema_by_id("ten_1", "sch_1").archived is True
    # 行は消えていない（過去の抽出結果から定義を辿れる）
    assert ctx.admin.get_schema_by_id("ten_1", "sch_1").fields
    audits = ctx.client.app.state.workflows.audits
    assert audits[-1]["action"] == "schema.archive" and audits[-1]["target_type"] == "schema"

    # 既定の一覧・doc-types から消える。include_archived=true でだけ見える
    assert ctx.client.get("/v1/schemas", headers=auth("admin")).json()["items"] == []
    assert ctx.client.get("/v1/doc-types", headers=auth("uploader")).json()["items"] == []
    shown = ctx.client.get("/v1/schemas?include_archived=true", headers=auth("admin")).json()
    assert [(s["doc_type"], s["archived"]) for s in shown["items"]] == [("invoice", True)]
    # doc_type 直引き（テンプレート化の編集モードの起点）は E1001
    r = ctx.client.get("/v1/schemas/invoice", headers=auth("admin"))
    assert r.status_code == 400 and r.json()["error"]["details"]["archived"] is True
    r = ctx.client.get("/v1/schemas/invoice?include_archived=true", headers=auth("admin"))
    assert r.status_code == 200 and r.json()["archived"] is True

    # 冪等
    assert ctx.client.post("/v1/schemas/invoice/archive", headers=auth("admin")).status_code == 200

    # 復元
    r = ctx.client.post("/v1/schemas/invoice/unarchive", headers=auth("admin"))
    assert r.status_code == 200 and r.json()["archived"] is False
    assert [s["doc_type"] for s in ctx.client.get("/v1/schemas", headers=auth("admin")).json()["items"]] == ["invoice"]
    assert ctx.admin.get_schema_by_id("ten_1", "sch_1_v3").archived is False
    assert audits[-1]["action"] == "schema.unarchive"


def test_アーカイブ済みには新版を足せない(ctx: SimpleNamespace) -> None:
    ctx.client.post("/v1/schemas/invoice/archive", headers=auth("admin"))
    fields = [{"name": "total_amount", "type": "money_jpy"}]
    # 編集（新版）も新規作成（同名）も E1005 で、復元を案内する
    for create in (False, True):
        r = ctx.client.put(
            "/v1/schemas", json={"doc_type": "invoice", "fields": fields, "create": create},
            headers=auth("admin"),
        )
        assert r.status_code == 409, r.text
        assert r.json()["error"]["details"]["archived"] is True
        assert "復元" in r.json()["error"]["message"]
    # 版が増えていない（アーカイブが黙って解除されていない）
    assert ctx.admin.get_schema("ten_1", "invoice").version == 4
    assert ctx.admin.get_schema("ten_1", "invoice").archived is True

    # chat 経路（put_schema 直呼び）は SchemaArchivedError（ValueError）で ok=False
    from newfan_gateway.chat_tools import ChatTools

    tools = ChatTools(repo=ctx.repo, admin=ctx.admin, queue=ctx.queue)
    out = tools.update_schema("ten_1", "invoice", {"name": "x", "type": "string"})
    assert out["ok"] is False and "復元" in out["message"]


def test_有効なワークフローが旧版を指していればアーカイブできない(ctx: SimpleNamespace) -> None:
    _seed_old_version(ctx)
    wid = _create_active_workflow(ctx)
    r = ctx.client.post("/v1/schemas/invoice/archive", headers=auth("admin"))
    assert r.status_code == 409, r.text
    err = r.json()["error"]
    assert err["code"] == "E1005" and err["details"]["reason"] == "workflow_active"
    assert [w["id"] for w in err["details"]["workflows"]] == [wid]
    assert ctx.admin.get_schema("ten_1", "invoice").archived is False

    # 停止すればアーカイブできる。そのワークフローは再有効化できない（L009）
    ctx.client.post(f"/v1/workflows/{wid}/pause", headers=auth("admin"))
    assert ctx.client.post("/v1/schemas/invoice/archive", headers=auth("admin")).status_code == 200
    r = ctx.client.post(f"/v1/workflows/{wid}/activate", headers=auth("admin"))
    assert r.status_code == 422
    findings = r.json()["error"]["details"]["findings"]
    assert [f["rule"] for f in findings] == ["L009"]
    assert "アーカイブ" in findings[0]["message"]
    # lint でも同じ指摘が出る（有効化ボタンを押す前に画面で分かる）
    lint = ctx.client.post(f"/v1/workflows/{wid}/lint", headers=auth("admin")).json()
    assert lint["activatable"] is False and [f["rule"] for f in lint["findings"]] == ["L009"]

    # 復元すれば有効化できる
    ctx.client.post("/v1/schemas/invoice/unarchive", headers=auth("admin"))
    assert ctx.client.post(f"/v1/workflows/{wid}/activate", headers=auth("admin")).status_code == 200


def _upload(ctx: SimpleNamespace) -> str:
    r = ctx.client.post(
        "/v1/documents",
        headers=auth("uploader"),
        files={"file": ("invoice.pdf", PDF, "application/pdf")},
    )
    assert r.status_code == 201, r.text
    return str(r.json()["document_id"])


def test_アーカイブ済みのschema_idでは新しい抽出を始められない(ctx: SimpleNamespace) -> None:
    # 一覧から隠すだけでは、document 画面の「再抽出」（run.schema_id を明示送信）や
    # API 直叩き・chat の rerun_extract で、アーカイブ済みの定義に新しい run が積める
    # （レビュー確定）。get_schema_by_id はアーカイブ済みも返す（過去の run から定義を
    # 辿るため）ので、抽出の入口で archived を見る
    from newfan_gateway.chat_tools import ChatTools

    doc_id = _upload(ctx)
    ctx.client.post("/v1/schemas/invoice/archive", headers=auth("admin"))

    r = ctx.client.post(
        f"/v1/documents/{doc_id}/extract", headers=auth("uploader"), json={"schema_id": "sch_1"}
    )
    assert r.status_code == 409, r.text
    err = r.json()["error"]
    assert err["code"] == "E1005" and err["details"]["archived"] is True
    assert err["details"]["doc_type"] == "invoice" and "復元" in err["message"]
    assert ctx.queue.messages == []  # run も job も積まれていない
    assert ctx.repo.get_latest_run("ten_1", doc_id) is None

    tools = ChatTools(repo=ctx.repo, admin=ctx.admin, queue=ctx.queue)
    out = tools.rerun_extract("ten_1", doc_id, schema_id="sch_1")
    assert out["ok"] is False and "復元" in out["message"]
    assert ctx.queue.messages == []
    # 存在しない id は従来どおり
    assert tools.rerun_extract("ten_1", doc_id, schema_id="sch_nope")["ok"] is False

    # 復元すれば通る
    ctx.client.post("/v1/schemas/invoice/unarchive", headers=auth("admin"))
    r = ctx.client.post(
        f"/v1/documents/{doc_id}/extract", headers=auth("uploader"), json={"schema_id": "sch_1"}
    )
    assert r.status_code == 202, r.text
    assert len(ctx.queue.messages) == 1


def test_無いdoc_typeとadmin以外(ctx: SimpleNamespace) -> None:
    assert ctx.client.post("/v1/schemas/nope/archive", headers=auth("admin")).status_code == 400
    assert ctx.client.post("/v1/schemas/invoice/archive", headers=auth("reviewer")).status_code == 403
    assert ctx.client.post("/v1/schemas/invoice/unarchive", headers=auth("reviewer")).status_code == 403
