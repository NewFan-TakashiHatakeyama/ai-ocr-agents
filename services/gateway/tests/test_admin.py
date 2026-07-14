"""管理画面 API（SCR-04/05/06）: スキーマ版管理・ルールライフサイクル・KPI・RBAC。"""

from __future__ import annotations

from types import SimpleNamespace

from gw_helpers import auth


# ---- スキーマ（SCR-06） ----


def test_list_schemas_admin(ctx: SimpleNamespace) -> None:
    r = ctx.client.get("/v1/schemas", headers=auth("admin"))
    assert r.status_code == 200
    items = r.json()["items"]
    assert items[0]["doc_type"] == "invoice" and items[0]["version"] == 4
    assert any(f["name"] == "total_amount" and f["critical"] for f in items[0]["fields"])


def test_get_schema_latest(ctx: SimpleNamespace) -> None:
    r = ctx.client.get("/v1/schemas/invoice", headers=auth("admin"))
    assert r.status_code == 200 and r.json()["version"] == 4


def test_put_schema_creates_new_version(ctx: SimpleNamespace) -> None:
    body = {
        "doc_type": "invoice",
        "fields": [
            {"name": "total_amount", "label": "合計金額(税込)", "type": "money_jpy", "critical": True},
            {"name": "payment_method", "label": "支払方法", "type": "string"},
        ],
    }
    r = ctx.client.put("/v1/schemas", headers=auth("admin"), json=body)
    assert r.status_code == 200
    assert r.json()["version"] == 5  # v4 → v5
    assert any(f["name"] == "payment_method" for f in r.json()["fields"])


def test_schemas_require_admin(ctx: SimpleNamespace) -> None:
    assert ctx.client.get("/v1/schemas", headers=auth("reviewer")).status_code == 403


# ---- ルール（SCR-05） ----


def test_list_rules(ctx: SimpleNamespace) -> None:
    r = ctx.client.get("/v1/rules", headers=auth("admin"))
    assert r.status_code == 200
    by_id = {x["id"]: x for x in r.json()["items"]}
    assert by_id["rul_ok"]["activatable"] is True
    assert by_id["rul_bad"]["activatable"] is False


def test_activate_rule_passes_validation(ctx: SimpleNamespace) -> None:
    r = ctx.client.patch("/v1/rules/rul_ok", headers=auth("admin"), json={"status": "active"})
    assert r.status_code == 200 and r.json()["status"] == "active"


def test_activate_rule_blocked_when_validation_fails(ctx: SimpleNamespace) -> None:
    # 再現率0.5・回帰2件 → 有効化不可（409）
    r = ctx.client.patch("/v1/rules/rul_bad", headers=auth("admin"), json={"status": "active"})
    assert r.status_code == 409


def test_retire_rule_always_allowed(ctx: SimpleNamespace) -> None:
    r = ctx.client.patch("/v1/rules/rul_bad", headers=auth("admin"), json={"status": "retired"})
    assert r.status_code == 200 and r.json()["status"] == "retired"


def test_rules_require_admin(ctx: SimpleNamespace) -> None:
    assert ctx.client.get("/v1/rules", headers=auth("reviewer")).status_code == 403


# ---- KPI（SCR-04） ----


def test_metrics_summary(ctx: SimpleNamespace) -> None:
    r = ctx.client.get("/v1/metrics/summary", headers=auth("admin"))
    assert r.status_code == 200
    body = r.json()
    assert body["pending_rules"] == 2  # rul_ok + rul_bad は draft
    assert "stp_rate" in body and "status_counts" in body


def test_metrics_require_admin(ctx: SimpleNamespace) -> None:
    assert ctx.client.get("/v1/metrics/summary", headers=auth("uploader")).status_code == 403
