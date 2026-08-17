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


# ---- 敵対的レビュー確定/保留所見の回帰（2026-08-17） ----


def test_agent生成のllm_hintは検証免除されない(ctx: SimpleNamespace) -> None:
    # 検証免除は「人が明示的に書いた指示」に限る。学習エージェント生成の llm_hint
    # まで免除すると、検証不合格ルールが1クリックで全 KIE プロンプトに注入される。
    from newfan_gateway.records import RuleRecord

    ctx.admin.create_rule(
        RuleRecord(
            id="rule_agent_hint",
            tenant_id="ten_1",
            doc_type="invoice",
            rule_type="llm_hint",
            rule_json={"hint_text": "agent が推測した指示"},
            status="draft",
            created_by="agent",
        )
    )
    rules = ctx.client.get("/v1/rules", headers=auth("admin")).json()["items"]
    target = next(r for r in rules if r["id"] == "rule_agent_hint")
    assert target["activatable"] is False
    act = ctx.client.patch("/v1/rules/rule_agent_hint", headers=auth("admin"), json={"status": "active"})
    assert act.status_code == 409
    assert act.json()["error"]["code"] == "E1006"


def test_llm_hintの本文長は上限あり(ctx: SimpleNamespace) -> None:
    # 無制限だと1件の巨大ヒントが全 KIE プロンプトを肥大化させ抽出を恒久失敗させ得る
    r = ctx.client.post(
        "/v1/rules",
        headers=auth("admin"),
        json={"doc_type": "invoice", "hint_text": "あ" * 2001},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "E1003"


def test_llm_hintは未登録doc_typeを拒否する(ctx: SimpleNamespace) -> None:
    # 未登録 doc_type は memory_lookup の等値一致に一度もヒットせず
    # 「有効なのに適用されない」サイレント故障になる
    r = ctx.client.post(
        "/v1/rules",
        headers=auth("admin"),
        json={"doc_type": "invoce", "hint_text": "typo した doc_type"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "E1001"
