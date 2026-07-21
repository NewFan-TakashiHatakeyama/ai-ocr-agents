"""チャットグラフ（Supervisor 型 LangGraph, §3.3 / §4.5）。

    supervisor（ルーティング）→ 各ツールノード → respond
    書込み系ツールは confirm_action（interrupt）を経由し、UI に確認カードを出す。

抽出グラフ（§4.1）とは別のグラフ。langgraph は gateway の runtime extra。

interrupt/resume は使わず、confirm_action で **グラフを止めて confirm_request を返す**。
承認は既存の `POST /v1/chat/confirm` が実行する（§4.5 の「承認後に実行」）。checkpointer を
持ち込まないのは、チャットは 1 往復で完結し、承認は別リクエストとして届くため。
"""

from __future__ import annotations

from typing import Annotated, Any, Callable, Optional, TypedDict

from newfan_gateway.chat_tools import WRITE_TOOLS, ChatTools

# LLM が選べるツール（§3.3）。書込み系は WRITE_TOOLS。
TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "get_result",
        "description": "指定ドキュメントの抽出結果（項目・確信度・状態）を取得する。",
        "input_schema": {
            "type": "object",
            "properties": {"document_id": {"type": "string"}},
            "required": ["document_id"],
        },
    },
    {
        "name": "explain_field",
        "description": "項目がどこから読まれたか（ページ・bbox・原文・grounding）を説明する。",
        "input_schema": {
            "type": "object",
            "properties": {
                "document_id": {"type": "string"},
                "field_name": {"type": "string"},
            },
            "required": ["document_id", "field_name"],
        },
    },
    {
        "name": "search_documents",
        "description": "ドキュメントを検索する。status で絞り込める（needs_review 等）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "limit": {"type": "integer"},
            },
        },
    },
    {
        "name": "list_rules",
        "description": "テナントのルール一覧を返す。status で絞り込める（draft/active 等）。",
        "input_schema": {
            "type": "object",
            "properties": {"status": {"type": "string"}},
        },
    },
    {
        "name": "navigate",
        "description": "ユーザーを指定画面へ案内する（読み取り操作）。",
        "input_schema": {
            "type": "object",
            "properties": {"target": {"type": "string"}, "label": {"type": "string"}},
            "required": ["target"],
        },
    },
    {
        "name": "rerun_extract",
        "description": "ドキュメントを再抽出する（新しい Run を発行。書込み操作・承認が必要）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "document_id": {"type": "string"},
                "schema_id": {"type": "string"},
                "prompt": {"type": "string", "description": "ユーザーへの確認文"},
            },
            "required": ["document_id"],
        },
    },
    {
        "name": "update_schema",
        "description": "抽出スキーマに項目を追加する（書込み操作・承認が必要）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "doc_type": {"type": "string"},
                "field": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "label": {"type": "string"},
                        "type": {"type": "string"},
                    },
                },
                "prompt": {"type": "string"},
            },
            "required": ["doc_type", "field"],
        },
    },
    {
        "name": "manage_rules",
        "description": "ルールを承認/却下する（書込み操作・承認が必要）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "rule_id": {"type": "string"},
                "status": {"type": "string", "enum": ["active", "rejected", "disabled"]},
                "prompt": {"type": "string"},
            },
            "required": ["rule_id", "status"],
        },
    },
]

READ_TOOL_NAMES = [t["name"] for t in TOOL_SPECS if t["name"] not in WRITE_TOOLS]


class ChatState(TypedDict, total=False):
    tenant_id: str
    message: str
    # supervisor が選んだツール（None なら respond へ）
    tool_name: Optional[str]
    tool_args: dict[str, Any]
    # 読み取りツールの実行結果（supervisor が次の判断に使う）
    observations: Annotated[list[dict[str, Any]], lambda a, b: (a or []) + (b or [])]
    # 出力
    answer: str
    confirm: Optional[dict[str, Any]]
    navigate: Optional[dict[str, Any]]


# supervisor に渡す LLM 呼び出し。(system, message, observations, tools) -> 決定
# 戻り値: {"tool": name|None, "args": {...}, "text": "..."}
SupervisorFn = Callable[[ChatState], dict[str, Any]]


def _route(state: ChatState) -> str:
    """supervisor の決定に従って次のノードを選ぶ。"""
    name = state.get("tool_name")
    if not name:
        return "respond"
    if name in WRITE_TOOLS:
        # 書込みは必ず確認を挟む（§3.3）
        return "confirm_action"
    return "execute_tool"


def build_chat_graph(*, supervisor: SupervisorFn, tools: ChatTools, max_steps: int = 4) -> Any:
    """Supervisor グラフを構築して compile 済みグラフを返す。

    max_steps: 読み取りツールのループ上限（LLM が延々とツールを呼ぶのを防ぐ）。
    """
    from langgraph.graph import END, START, StateGraph  # 遅延 import（runtime extra）

    def supervisor_node(state: ChatState) -> dict[str, Any]:
        if len(state.get("observations", [])) >= max_steps:
            # 打ち切り: 集めた観測で答えさせる
            return {"tool_name": None}
        decision = supervisor(state)
        return {
            "tool_name": decision.get("tool"),
            "tool_args": decision.get("args") or {},
            "answer": decision.get("text") or state.get("answer", ""),
        }

    def execute_tool_node(state: ChatState) -> dict[str, Any]:
        name = state.get("tool_name") or ""
        args = dict(state.get("tool_args") or {})
        tenant_id = state.get("tenant_id", "")

        if name == "navigate":
            # 画面遷移は副作用が無く、そのまま UI へ返す
            return {"navigate": args, "observations": [{"tool": name, "result": args}]}

        fn = {
            "get_result": lambda: tools.get_result(tenant_id, str(args.get("document_id", ""))),
            "explain_field": lambda: tools.explain_field(
                tenant_id, str(args.get("document_id", "")), str(args.get("field_name", ""))
            ),
            "search_documents": lambda: tools.search_documents(
                tenant_id, status=args.get("status"), limit=int(args.get("limit", 20))
            ),
            "list_rules": lambda: tools.list_rules(tenant_id, status=args.get("status")),
        }.get(name)
        if fn is None:
            return {"observations": [{"tool": name, "result": {"error": "未知のツール"}}]}
        return {"observations": [{"tool": name, "result": fn()}]}

    def confirm_action_node(state: ChatState) -> dict[str, Any]:
        """書込みツールは実行せず、確認カードを返してグラフを終える（§4.5）。

        実行は承認後に POST /v1/chat/confirm が行う。ここで実行してしまうと
        「実行前にユーザー確認」という要件（§3.3）を破る。
        """
        args = dict(state.get("tool_args") or {})
        prompt = args.pop("prompt", None)
        return {
            "confirm": {"action": state.get("tool_name"), **args, "prompt": prompt},
            "tool_name": None,
        }

    def respond_node(state: ChatState) -> dict[str, Any]:
        if state.get("answer"):
            return {}
        return {"answer": "お手伝いできることがあれば教えてください。"}

    g: Any = StateGraph(ChatState)
    g.add_node("supervisor", supervisor_node)
    g.add_node("execute_tool", execute_tool_node)
    g.add_node("confirm_action", confirm_action_node)
    g.add_node("respond", respond_node)

    g.add_edge(START, "supervisor")
    g.add_conditional_edges(
        "supervisor",
        _route,
        {"execute_tool": "execute_tool", "confirm_action": "confirm_action", "respond": "respond"},
    )
    # 読み取りツールの後は supervisor に戻す（観測を見て次を決める / 答える）
    g.add_edge("execute_tool", "supervisor")
    g.add_edge("confirm_action", END)
    g.add_edge("respond", END)
    return g.compile()
