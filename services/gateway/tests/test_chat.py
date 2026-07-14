"""チャットホーム API（SCR-01）: SSE イベント・承認実行・RBAC。"""

from __future__ import annotations

import json
from types import SimpleNamespace

from gw_helpers import auth


def _sse(text: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    ev = None
    for line in text.splitlines():
        if line.startswith("event: "):
            ev = line[len("event: ") :]
        elif line.startswith("data: ") and ev is not None:
            events.append((ev, json.loads(line[len("data: ") :])))
    return events


def test_chat_review_intent_streams_navigate(ctx: SimpleNamespace) -> None:
    r = ctx.client.post("/v1/chat", headers=auth("viewer"), json={"message": "要確認の請求書を見せて"})
    assert r.status_code == 200
    evs = _sse(r.text)
    types = [t for t, _ in evs]
    assert "token" in types and types[-1] == "done"
    tool = [d for t, d in evs if t == "tool_call"][0]
    assert tool["target"] == "/documents?tab=queue"


def test_chat_schema_add_intent_emits_confirm(ctx: SimpleNamespace) -> None:
    r = ctx.client.post(
        "/v1/chat", headers=auth("viewer"), json={"message": "スキーマに「支払方法」を追加して"}
    )
    evs = _sse(r.text)
    conf = [d for t, d in evs if t == "confirm_request"]
    assert conf, "書込み系は confirm_request を挟む（§4.5）"
    assert conf[0]["action"] == "update_schema"
    assert conf[0]["field"]["label"] == "支払方法"


def test_chat_confirm_update_schema_creates_new_version(ctx: SimpleNamespace) -> None:
    # 既存 invoice v4 に「支払方法」を追加 → v5
    body = {
        "action": "update_schema",
        "params": {"doc_type": "invoice", "field": {"name": "payment_method", "label": "支払方法", "type": "string"}},
    }
    r = ctx.client.post("/v1/chat/confirm", headers=auth("admin"), json=body)
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["detail"]["version"] == 5
    # スキーマに反映
    s = ctx.client.get("/v1/schemas/invoice", headers=auth("admin")).json()
    assert any(f["name"] == "payment_method" for f in s["fields"])


def test_chat_confirm_requires_admin(ctx: SimpleNamespace) -> None:
    body = {"action": "update_schema", "params": {"doc_type": "invoice", "field": {"name": "x", "label": "X"}}}
    assert ctx.client.post("/v1/chat/confirm", headers=auth("reviewer"), json=body).status_code == 403


def test_chat_requires_auth(ctx: SimpleNamespace) -> None:
    assert ctx.client.post("/v1/chat", json={"message": "hi"}).status_code == 403
