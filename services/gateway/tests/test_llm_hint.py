"""LLM最適化ヒント（llm_hint）のオーサリング→承認（③）。"""

from types import SimpleNamespace

from gw_helpers import auth


def test_llm_hintを作成すると承認待ちで検証不要に有効化できる(ctx: SimpleNamespace) -> None:
    r = ctx.client.post(
        "/v1/rules",
        headers=auth("admin"),
        json={
            "doc_type": "invoice",
            "field_name": "issuer_name",
            "hint_text": "取引先名は右上の会社名を優先し、敬称『御中』は除去する。",
            "description": "取引先名の取り違え対策",
        },
    )
    assert r.status_code == 201, r.text
    rule = r.json()
    assert rule["rule_type"] == "llm_hint"
    assert rule["status"] == "draft"
    assert rule["rule_json"]["hint_text"].startswith("取引先名")
    # llm_hint は検証レポート無しでも有効化できる
    assert rule["activatable"] is True

    rid = rule["id"]
    act = ctx.client.patch(f"/v1/rules/{rid}", headers=auth("admin"), json={"status": "active"})
    assert act.status_code == 200, act.text
    assert act.json()["status"] == "active"


def test_llm_hintは本文必須(ctx: SimpleNamespace) -> None:
    r = ctx.client.post(
        "/v1/rules",
        headers=auth("admin"),
        json={"doc_type": "invoice", "hint_text": "   "},
    )
    assert r.status_code == 422  # E1003
    assert r.json()["error"]["code"] == "E1003"


def test_llm_hint作成はadmin限定(ctx: SimpleNamespace) -> None:
    r = ctx.client.post(
        "/v1/rules",
        headers=auth("reviewer"),
        json={"doc_type": "invoice", "hint_text": "x"},
    )
    assert r.status_code == 403


def test_学習ルールは従来どおり検証未達で有効化を拒否(ctx: SimpleNamespace) -> None:
    # llm_hint 以外（regex_replace）は検証レポート無しでは有効化できない（回帰）
    from newfan_gateway.records import RuleRecord

    ctx.admin.create_rule(
        RuleRecord(
            id="rule_regex_x",
            tenant_id="ten_1",
            doc_type="invoice",
            rule_type="regex_replace",
            rule_json={"pattern": "a", "replacement": "b"},
            status="draft",
            created_by="agent",
        )
    )
    act = ctx.client.patch("/v1/rules/rule_regex_x", headers=auth("admin"), json={"status": "active"})
    assert act.status_code == 409  # E1006（検証未達）
    assert act.json()["error"]["code"] == "E1006"
