"""チャットグラフ（Supervisor, §3.3 / §4.5）。

以前のチャットは navigate / update_schema の 2 ツールしか持たず、設計書の
get_result / explain_field / rerun_extract / search_documents / manage_rules と
supervisor / confirm_action が丸ごと無かった。
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("langgraph", reason="チャットグラフは runtime extra")

from newfan_gateway.admin import InMemoryAdminRepository  # noqa: E402
from newfan_gateway.chat_graph import build_chat_graph  # noqa: E402
from newfan_gateway.chat_tools import ChatTools  # noqa: E402
from newfan_gateway.queue import InMemoryQueue  # noqa: E402
from newfan_gateway.records import (  # noqa: E402
    DocumentRecord,
    PageRecord,
    RuleRecord,
    RunRecord,
)
from newfan_gateway.repository import InMemoryRepository  # noqa: E402
from newfan_schemas import ExtractedField  # noqa: E402


def _tools() -> tuple[ChatTools, InMemoryRepository, InMemoryAdminRepository, InMemoryQueue]:
    repo, admin, queue = InMemoryRepository(), InMemoryAdminRepository(), InMemoryQueue()
    repo.create_document(
        DocumentRecord(
            id="doc_1", tenant_id="ten_1", storage_uri="s3://b/k", mime_type="image/png",
            page_count=1, doc_type="invoice", status="needs_review",
        ),
        [PageRecord(page_no=1, width=740, height=1046, image_uri="s3://b/p1.png")],
    )
    repo.create_run(
        RunRecord(
            id="run_1", tenant_id="ten_1", document_id="doc_1", status="needs_review",
            fields=[
                ExtractedField(
                    name="合計金額", value_raw="7,003", value_normalized="7003",
                    confidence=1.0, grounding_score=1.0, page=1,
                    bbox=[100, 200, 180, 220], source_quote="¥ 7,003-",
                )
            ],
        )
    )
    admin.seed_rule(
        RuleRecord(
            id="rul_1", tenant_id="ten_1", doc_type="invoice", field_name="合計金額",
            rule_type="regex_replace", rule_json={"pattern": "x"}, status="draft",
        )
    )
    return ChatTools(repo=repo, admin=admin, queue=queue), repo, admin, queue


def _graph(decisions: list[dict[str, Any]], tools: ChatTools) -> Any:
    """decisions を順に返す決定論 supervisor でグラフを組む。"""
    seq = list(decisions)

    def supervisor(_state: Any) -> dict[str, Any]:
        return seq.pop(0) if seq else {"tool": None, "args": {}, "text": "done"}

    return build_chat_graph(supervisor=supervisor, tools=tools)


def test_get_result_tool_reads_real_values() -> None:
    """チャットが実際の抽出結果を読めること（従来は読む手段が無かった）。"""
    tools, *_ = _tools()
    g = _graph(
        [
            {"tool": "get_result", "args": {"document_id": "doc_1"}, "text": ""},
            {"tool": None, "args": {}, "text": "合計金額は 7,003 円です。"},
        ],
        tools,
    )
    out = g.invoke({"tenant_id": "ten_1", "message": "doc_1 の合計金額は？"})

    obs = out["observations"]
    assert obs[0]["tool"] == "get_result"
    assert obs[0]["result"]["fields"][0]["value"] == "7003"
    assert out["answer"] == "合計金額は 7,003 円です。"
    assert out.get("confirm") is None


def test_explain_field_returns_grounding() -> None:
    tools, *_ = _tools()
    g = _graph(
        [
            {
                "tool": "explain_field",
                "args": {"document_id": "doc_1", "field_name": "合計金額"},
                "text": "",
            },
            {"tool": None, "args": {}, "text": "1ページ目から読み取りました。"},
        ],
        tools,
    )
    out = g.invoke({"tenant_id": "ten_1", "message": "根拠は？"})
    r = out["observations"][0]["result"]
    assert r["bbox"] == [100, 200, 180, 220]
    assert r["source_quote"] == "¥ 7,003-"
    assert r["grounding_score"] == 1.0


def test_search_documents_filters_by_status() -> None:
    tools, *_ = _tools()
    g = _graph(
        [
            {"tool": "search_documents", "args": {"status": "needs_review"}, "text": ""},
            {"tool": None, "args": {}, "text": "1 件あります。"},
        ],
        tools,
    )
    out = g.invoke({"tenant_id": "ten_1", "message": "要確認は？"})
    assert out["observations"][0]["result"]["count"] == 1


def test_rerun_extract_requires_confirmation_and_does_not_run() -> None:
    """書込み系は確認前に実行してはいけない（§3.3）。"""
    tools, _repo, _admin, queue = _tools()
    g = _graph(
        [
            {
                "tool": "rerun_extract",
                "args": {"document_id": "doc_1", "prompt": "再抽出しますか？"},
                "text": "承認をお願いします。",
            }
        ],
        tools,
    )
    out = g.invoke({"tenant_id": "ten_1", "message": "doc_1 を再抽出して"})

    assert out["confirm"]["action"] == "rerun_extract"
    assert out["confirm"]["document_id"] == "doc_1"
    assert out["confirm"]["prompt"] == "再抽出しますか？"
    # 承認前なのでキューには入れない
    assert queue.messages == []


def test_update_schema_requires_confirmation() -> None:
    tools, _repo, admin, _q = _tools()
    g = _graph(
        [
            {
                "tool": "update_schema",
                "args": {
                    "doc_type": "invoice",
                    "field": {"name": "note", "label": "備考", "type": "string"},
                    "prompt": "追加しますか？",
                },
                "text": "承認をお願いします。",
            }
        ],
        tools,
    )
    out = g.invoke({"tenant_id": "ten_1", "message": "備考を追加して"})
    assert out["confirm"]["action"] == "update_schema"
    # 承認前にスキーマを書き換えない
    assert admin.get_schema("ten_1", "invoice") is None


def test_manage_rules_requires_confirmation() -> None:
    tools, _repo, admin, _q = _tools()
    g = _graph(
        [
            {
                "tool": "manage_rules",
                "args": {"rule_id": "rul_1", "status": "active", "prompt": "有効化しますか？"},
                "text": "承認をお願いします。",
            }
        ],
        tools,
    )
    out = g.invoke({"tenant_id": "ten_1", "message": "rul_1 を有効化して"})
    assert out["confirm"]["action"] == "manage_rules"
    assert admin.get_rule("ten_1", "rul_1").status == "draft"  # 未変更


def test_rerun_extract_runs_after_confirmation() -> None:
    """承認後（chat/confirm 経路）はツールが実際に Run を発行する。

    seed した run は needs_review。チャット再抽出の主用途が「レビュー中の帳票を
    スキーマを直して取り直す」なので、needs_review は競合として弾かない（§4.5）。
    """
    tools, _repo, _admin, queue = _tools()
    res = tools.rerun_extract("ten_1", "doc_1")
    assert res["ok"] is True and res["job_id"]
    assert len(queue.messages) == 1 and queue.messages[0][0] == "q.extract"


def test_rerun_extract_rejects_while_processing() -> None:
    """処理中の二重起動は拒否する。"""
    tools, repo, _admin, queue = _tools()
    repo.create_run(
        RunRecord(id="run_2", tenant_id="ten_1", document_id="doc_1", status="processing")
    )
    res = tools.rerun_extract("ten_1", "doc_1")
    assert res["ok"] is False
    assert "処理中" in res["message"]
    assert queue.messages == []


def test_rerun_extract_rejects_unknown_document() -> None:
    tools, *_ = _tools()
    assert tools.rerun_extract("ten_1", "doc_missing")["ok"] is False


def test_supervisor_loop_is_bounded() -> None:
    """LLM がツールを呼び続けても打ち切ること。"""
    tools, *_ = _tools()
    g = build_chat_graph(
        supervisor=lambda _s: {"tool": "search_documents", "args": {}, "text": "..."},
        tools=tools,
        max_steps=2,
    )
    out = g.invoke({"tenant_id": "ten_1", "message": "loop"})
    assert len(out["observations"]) == 2
