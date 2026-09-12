"""チャットホーム API（SCR-01）: SSE イベント・承認実行・RBAC。"""

from __future__ import annotations

import json
from types import SimpleNamespace

from gw_helpers import PDF, auth


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
    assert conf[0]["doc_type"] == "invoice"  # 対象を書いていなければ従来どおり invoice


def test_chat_schema_add_intent_targets_named_doc_type(ctx: SimpleNamespace) -> None:
    """「<doc_type> のスキーマに…」なら、その doc_type を承認カードに載せる。

    スキーマ管理画面の「チャットで追加を依頼」は開いていたスキーマ名をこの形で
    渡してくる。常に invoice に固定すると、delivery_note を開いていた管理者の依頼が
    invoice の新版になる（テナントに invoice が無ければ 1 項目だけの新規スキーマ）。
    """
    for message, expected in [
        ("delivery_note のスキーマに「支払方法」を追加して", "delivery_note"),
        ("発注書のスキーマに「担当者」を足して", "発注書"),
    ]:
        r = ctx.client.post("/v1/chat", headers=auth("viewer"), json={"message": message})
        conf = [d for t, d in _sse(r.text) if t == "confirm_request"]
        assert conf and conf[0]["doc_type"] == expected, message
        # 承認前に対象が読めるよう、確認文にもスキーマ名を入れる
        assert expected in conf[0]["prompt"]


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


def test_chat_tools_update_schema_rejects_reserved_name_without_raising(
    ctx: SimpleNamespace,
) -> None:
    """予約名（__pages__ / __region__ / 先頭 __）は ok=False で返し、例外にしない。

    設計 region-field-add-and-hint-v2 D9。UI の命名規則は英字始まりなので通らないが、
    チャット経路は LLM の dict をそのまま SchemaFieldDef に組む。ValidationError を
    素通しするとグラフごと落ちて会話が途切れるので、ツールの戻り値で断る。
    """
    from newfan_gateway.chat_tools import ChatTools

    tools = ChatTools(repo=ctx.repo, admin=ctx.admin, queue=ctx.queue)
    before = ctx.admin.get_schema("ten_1", "invoice").version
    for name in ("__pages__", "__region__", "__memo"):
        res = tools.update_schema("ten_1", "invoice", {"name": name, "label": "備考"})
        assert res["ok"] is False, name
        assert "予約" in res["message"]
    # 型違い（LLM が bool 以外を渡す）も同じ経路で断る
    res = tools.update_schema("ten_1", "invoice", {"name": "memo", "required": "maybe"})
    assert res["ok"] is False and "required" in res["message"]
    # FieldType に無い型（LLM の言い間違い）も put_schema が ValueError で拒み、ok=False で返す。
    # 素通しすると orchestrator が実行時に落として、その doc_type の抽出が全部 failed になる
    res = tools.update_schema("ten_1", "invoice", {"name": "memo", "type": "addres_jp"})
    assert res["ok"] is False and "型" in res["message"]
    # 断った呼び出しは新版を作らない
    assert ctx.admin.get_schema("ten_1", "invoice").version == before


def test_chat_tools_search_documents_includes_original_name(ctx: SimpleNamespace) -> None:
    """search_documents は原本ファイル名を返す（ID だけでは利用者が突き合わせられない）。"""
    from newfan_gateway.chat_tools import ChatTools

    r = ctx.client.post(
        "/v1/documents",
        headers=auth("uploader"),
        files={"file": ("納品書_0912.pdf", PDF, "application/pdf")},
    )
    assert r.status_code == 201, r.text
    tools = ChatTools(repo=ctx.repo, admin=ctx.admin, queue=ctx.queue)
    res = tools.search_documents("ten_1")
    assert res["count"] == 1
    assert res["items"][0]["original_name"] == "納品書_0912.pdf"


def test_chat_confirm_update_schema_reserved_name_returns_ok_false(ctx: SimpleNamespace) -> None:
    """/chat/confirm も SchemaFieldDef を直接組む。500（E2000）ではなく ok=False。"""
    body = {
        "action": "update_schema",
        "params": {"doc_type": "invoice", "field": {"name": "__region__", "label": "領域"}},
    }
    r = ctx.client.post("/v1/chat/confirm", headers=auth("admin"), json=body)
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert "予約" in r.json()["message"]
    s = ctx.client.get("/v1/schemas/invoice", headers=auth("admin")).json()
    assert s["version"] == 4 and not any(f["name"] == "__region__" for f in s["fields"])


def test_chat_requires_auth(ctx: SimpleNamespace) -> None:
    assert ctx.client.post("/v1/chat", json={"message": "hi"}).status_code == 403


# ---- 本番エージェント: Anthropic ストリーム→SSE 写像 ----

from newfan_gateway.chat import LlmChatAgent, map_events  # noqa: E402


def _ns(**kw: object) -> SimpleNamespace:
    return SimpleNamespace(**kw)


def _navigate_events() -> list[SimpleNamespace]:
    return [
        _ns(type="content_block_start", content_block=_ns(type="text")),
        _ns(type="content_block_delta", delta=_ns(type="text_delta", text="ダッシュボードを開きます。")),
        _ns(type="content_block_stop"),
        _ns(type="content_block_start", content_block=_ns(type="tool_use", name="navigate")),
        _ns(type="content_block_delta", delta=_ns(type="input_json_delta", partial_json='{"target":"/dashboard",')),
        _ns(type="content_block_delta", delta=_ns(type="input_json_delta", partial_json='"label":"ダッシュボード"}')),
        _ns(type="content_block_stop"),
        _ns(type="message_stop"),
    ]


def test_map_events_navigate() -> None:
    evs = list(map_events(iter(_navigate_events())))
    types = [e.type for e in evs]
    assert types == ["token", "tool_call", "done"]
    assert evs[0].data["text"] == "ダッシュボードを開きます。"
    assert evs[1].data == {"name": "navigate", "target": "/dashboard", "label": "ダッシュボード"}


def test_map_events_update_schema_is_confirm() -> None:
    events = [
        _ns(type="content_block_start", content_block=_ns(type="tool_use", name="update_schema")),
        _ns(type="content_block_delta", delta=_ns(type="input_json_delta", partial_json='{"doc_type":"invoice","field":{"name":"note","label":"備考"},"prompt":"追加しますか？"}')),
        _ns(type="content_block_stop"),
        _ns(type="message_stop"),
    ]
    evs = list(map_events(iter(events)))
    assert [e.type for e in evs] == ["confirm_request", "done"]
    assert evs[0].data["action"] == "update_schema" and evs[0].data["field"]["label"] == "備考"


class _FakeStream:
    def __init__(self, events: list[SimpleNamespace]) -> None:
        self._events = events

    def __enter__(self) -> object:
        return iter(self._events)

    def __exit__(self, *a: object) -> bool:
        return False


class _FakeClient:
    def __init__(self, events: list[SimpleNamespace]) -> None:
        self.messages = SimpleNamespace(stream=lambda **kw: _FakeStream(events))


def test_llm_chat_agent_streams_via_client() -> None:
    agent = LlmChatAgent(client=_FakeClient(_navigate_events()))
    evs = list(agent.stream("ten_1", "先月のSTP率は？"))
    assert [e.type for e in evs] == ["token", "tool_call", "done"]


# ---- 本番エージェント（Gemini）: streaming + function calling → SSE 写像 ----

from newfan_gateway.chat import GeminiChatAgent, map_gemini_stream  # noqa: E402


def _chunk(parts: list[SimpleNamespace]) -> SimpleNamespace:
    return _ns(candidates=[_ns(content=_ns(parts=parts))])


def _text_part(t: str) -> SimpleNamespace:
    return _ns(text=t, function_call=None)


def _fc_part(name: str, args: dict) -> SimpleNamespace:
    return _ns(text=None, function_call=_ns(name=name, args=args))


def test_map_gemini_navigate_and_text() -> None:
    chunks = [
        _chunk([_text_part("ダッシュボードを開きます。")]),
        _chunk([_fc_part("navigate", {"target": "/dashboard", "label": "ダッシュボード"})]),
    ]
    evs = list(map_gemini_stream(iter(chunks)))
    assert [e.type for e in evs] == ["token", "tool_call", "done"]
    assert evs[1].data == {"name": "navigate", "target": "/dashboard", "label": "ダッシュボード"}


def test_map_gemini_update_schema_is_confirm() -> None:
    chunks = [_chunk([_fc_part("update_schema", {"doc_type": "invoice", "field": {"name": "note", "label": "備考"}, "prompt": "追加しますか？"})])]
    evs = list(map_gemini_stream(iter(chunks)))
    assert [e.type for e in evs] == ["confirm_request", "done"]
    assert evs[0].data["action"] == "update_schema" and evs[0].data["field"]["label"] == "備考"


def test_gemini_chat_agent_via_fake_client() -> None:
    chunks = [_chunk([_fc_part("navigate", {"target": "/rules"})])]
    client = _ns(models=_ns(generate_content_stream=lambda **kw: iter(chunks)))
    agent = GeminiChatAgent(client=client)
    evs = list(agent.stream("ten_1", "ルールを見せて"))
    assert [e.type for e in evs] == ["tool_call", "done"]
    assert evs[0].data["target"] == "/rules"
