"""Supervisor グラフを ChatAgent（SSE）に写像する（§3.3 / §4.5）。

グラフの実行結果を token / tool_call / confirm_request / done へ落とす。
既存の SSE 契約は変えない（web を触らずに supervisor へ載せ替えられる）。

supervisor の判断は LLMProvider（llm-adapter）に委ねる。プロバイダは
Anthropic / Gemini どちらでもよい（worker_main と同じ選択規則）。
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterator, Optional

from newfan_metrics import llm_tokens_total, tenant_scope

from newfan_gateway.chat import ChatEvent, _tokens
from newfan_gateway.chat_graph import TOOL_SPECS, ChatState, build_chat_graph
from newfan_gateway.chat_tools import ChatTools

_SYSTEM = """あなたは AI-OCR の運用を助けるアシスタントです。
ユーザーの質問に答えるために必要なら 1 つだけツールを選びます。

出力は必ず JSON のみ:
  {"tool": "<ツール名 or null>", "args": {...}, "text": "<ユーザーへの日本語の返答>"}

規則:
- 情報が足りないときはツールを呼んで観測を得る（observations に入る）。
- 観測が十分なら tool=null にして text で日本語で答える。数値や項目名は観測の値をそのまま使い、
  推測で補わない。観測に無いことは「わかりません」と答える。
- 書込み系（rerun_extract / update_schema / manage_rules）は args.prompt に確認文を入れる。
  実行はユーザー承認後なので、text では「承認をお願いします」と伝える。
"""


def _extract_json(text: str) -> dict[str, Any]:
    """```json で括られていても拾う。"""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("JSON が見つかりません")
    obj = json.loads(m.group(0))
    if not isinstance(obj, dict):
        raise ValueError("JSON オブジェクトではありません")
    return obj


class SupervisorChatAgent:
    """LangGraph Supervisor で動く本番チャットエージェント（§4.5）。"""

    def __init__(self, *, provider: Any, tools: ChatTools, max_tokens: int = 1024) -> None:
        self._provider = provider
        self._graph = build_chat_graph(supervisor=self._decide, tools=tools)
        self._max_tokens = max_tokens

    def _decide(self, state: ChatState) -> dict[str, Any]:
        user = json.dumps(
            {
                "message": state.get("message", ""),
                "observations": state.get("observations", []),
                "tools": [
                    {"name": t["name"], "description": t["description"], "args": t["input_schema"]}
                    for t in TOOL_SPECS
                ],
            },
            ensure_ascii=False,
        )
        resp = self._provider.complete(system=_SYSTEM, user=user, max_tokens=self._max_tokens)
        # §12.1 llm_tokens_total{purpose}。チャットは LLMAdapter を経由せず provider を
        # 直接叩くため、計上もここで行う（adapter._account を通らない）。
        llm_tokens_total.labels(purpose="chat", direction="input").inc(resp.input_tokens)
        llm_tokens_total.labels(purpose="chat", direction="output").inc(resp.output_tokens)
        try:
            data = _extract_json(resp.text)
        except (ValueError, json.JSONDecodeError):
            # JSON を返せなかったら素のテキストを回答として扱う（ツールは呼ばない）
            return {"tool": None, "args": {}, "text": resp.text.strip()}
        tool = data.get("tool")
        return {
            "tool": tool if isinstance(tool, str) and tool else None,
            "args": data.get("args") if isinstance(data.get("args"), dict) else {},
            "text": data.get("text") or "",
        }

    def stream(self, tenant_id: str, message: str) -> Iterator[ChatEvent]:
        with tenant_scope(tenant_id):
            final: dict[str, Any] = self._graph.invoke(
                {"tenant_id": tenant_id, "message": message}
            )

        # 使ったツールを進捗として見せる（§3.3 の tool_call）
        for obs in final.get("observations") or []:
            yield ChatEvent("tool_call", {"name": obs.get("tool"), "result": obs.get("result")})

        nav: Optional[dict[str, Any]] = final.get("navigate")
        if nav:
            yield ChatEvent("tool_call", {"name": "navigate", **nav})

        text = final.get("answer") or ""
        if text:
            yield from _tokens(text)

        confirm = final.get("confirm")
        if confirm:
            yield ChatEvent("confirm_request", {k: v for k, v in confirm.items() if v is not None})
            yield ChatEvent("done", {"reason": "confirm_pending"})
            return

        yield ChatEvent("done", {})
