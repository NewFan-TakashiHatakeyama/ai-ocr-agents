"""チャットエージェント（SCR-01, §3.3/§4.5）。

SSE イベント: token（逐次テキスト）/ tool_call（ノード進捗・画面遷移）/
confirm_request（書込み系ツールの承認要求）/ done。書込み（update_schema 等）は
必ず confirm_request→承認を挟む。dev/テストは RuleBasedChatAgent（決定論）、本番は
LLM ツール使用のエージェント（別途）を注入する。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterator, Protocol


@dataclass
class ChatEvent:
    type: str  # token | tool_call | confirm_request | done
    data: dict[str, Any] = field(default_factory=dict)


class ChatAgent(Protocol):
    def stream(self, tenant_id: str, message: str) -> Iterator[ChatEvent]: ...


def _tokens(text: str) -> Iterator[ChatEvent]:
    # 文節単位でトークンストリーム風に分割（UX 上の逐次表示用）
    for chunk in re.findall(r"[^、。\n]+[、。\n]?", text):
        yield ChatEvent("token", {"text": chunk})


class RuleBasedChatAgent:
    """意図をルールで判定する決定論エージェント（dev/テスト・デモ用）。"""

    def stream(self, tenant_id: str, message: str) -> Iterator[ChatEvent]:
        m = message.strip()

        # 書込み系: スキーマに項目追加 → 承認カード（§4.5 update_schema）
        if ("スキーマ" in m or "項目" in m) and ("追加" in m or "足し" in m):
            quoted = re.search(r"[「『](.+?)[」』]", m)
            label = quoted.group(1) if quoted else "支払方法"
            name = _slug(label)
            yield from _tokens(f"承知しました。請求書スキーマに「{label}」を追加して再抽出する提案です。")
            yield ChatEvent(
                "confirm_request",
                {
                    "action": "update_schema",
                    "doc_type": "invoice",
                    "field": {"name": name, "label": label, "type": "string"},
                    "prompt": f"スキーマに「{label}」を追加して再抽出しますか？（新しい版とRunを発行します）",
                },
            )
            yield ChatEvent("done", {"reason": "confirm_pending"})
            return

        # レビュー導線
        if any(k in m for k in ("要確認", "レビュー", "確認", "見せて")):
            yield from _tokens("要確認の帳票をレビューキューにまとめました。優先度順に確認できます。")
            yield ChatEvent(
                "tool_call",
                {"name": "navigate", "target": "/documents?tab=queue", "label": "レビューキューを開く"},
            )
            yield ChatEvent("done", {})
            return

        # KPI / ダッシュボード
        if any(k in m for k in ("STP", "率", "コスト", "精度", "ダッシュボード", "KPI")):
            yield from _tokens("KPI はダッシュボードで確認できます。STP率・処理内訳・学習の効きを表示します。")
            yield ChatEvent(
                "tool_call",
                {"name": "navigate", "target": "/dashboard", "label": "ダッシュボードを開く"},
            )
            yield ChatEvent("done", {})
            return

        # 抽出
        if any(k in m for k in ("抽出", "OCR", "読み取", "まとめて")):
            yield from _tokens("抽出を開始します。OCR → 項目抽出 → 検証 の順に進みます。")
            yield ChatEvent(
                "tool_call",
                {"name": "progress", "steps": ["OCR", "項目抽出", "検証"], "run_hint": "run_xxxx"},
            )
            yield ChatEvent("done", {})
            return

        # 既定: ヘルプ
        yield from _tokens(
            "帳票をドラッグ＆ドロップするか、日本語で指示してください。"
            "例:『要確認の請求書を見せて』『スキーマに「支払方法」を追加して』『先月のSTP率は？』"
        )
        yield ChatEvent("done", {})


def _slug(label: str) -> str:
    table = {"支払方法": "payment_method", "備考": "note", "担当者": "person_in_charge"}
    return table.get(label, "field_" + str(abs(hash(label)) % 10000))
