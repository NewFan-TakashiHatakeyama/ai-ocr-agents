# mypy: ignore-errors
"""複数トリガー WF の「発火経路のみ実行」（敵対的レビュー確定 major の残課題）。

従来は全トリガーへ無条件に START 辺を張っていたため、どのトリガーが発火しても
全経路が実行され、経路を分けたつもりの構成で DB 二重書込み・webhook 二重送信が
起きた。runner が workflow_runs.trigger.node_id を初期 state（fired_trigger）に載せ、
グラフは複数トリガー時のみ条件付きエントリで発火経路だけを走らせる。
fired_trigger が無い旧 run は従来どおり全経路（後方互換）。
"""

from __future__ import annotations

from typing import Any, Optional

import pytest

pytest.importorskip("langgraph.checkpoint.memory")

from langgraph.checkpoint.memory import MemorySaver  # noqa: E402

from newfan_orchestrator.consumer import InMemoryQueueConsumer  # noqa: E402
from newfan_orchestrator.workflow_graph import RunnerDeps  # noqa: E402
from newfan_orchestrator.workflow_runner import WorkflowRunner  # noqa: E402
from newfan_orchestrator.workflow_store import InMemoryWorkflowRunStore  # noqa: E402

TENANT = "ten_1"

# 経路A: 手動トリガー → 通知A / 経路B: gdrive トリガー → 通知B
GRAPH: dict[str, Any] = {
    "version": 1,
    "nodes": [
        {"id": "t1", "type": "source.manual", "config": {}},
        {"id": "t2", "type": "source.gdrive_event", "config": {"connection_id": "con_gd"}},
        {"id": "n1", "type": "sink.notify",
         "config": {"connection_id": "con_slack", "template": "経路A"}},
        {"id": "n2", "type": "sink.notify",
         "config": {"connection_id": "con_slack", "template": "経路B"}},
    ],
    "edges": [{"from": "t1", "to": "n1"}, {"from": "t2", "to": "n2"}],
}


def _run(trigger_node_id: Optional[str]) -> tuple[str, InMemoryWorkflowRunStore, list[str]]:
    store = InMemoryWorkflowRunStore()
    store.seed_run(
        "wfrun_ft", tenant_id=TENANT, workflow_id="wf_ft", graph_json=GRAPH,
        trigger_node_id=trigger_node_id,
    )
    store.seed_webhook(TENANT, "con_slack", "https://hooks.example/slack", "")
    sent: list[str] = []
    runner = WorkflowRunner(
        store,
        InMemoryQueueConsumer(),
        RunnerDeps(
            store=store,
            send_webhook=lambda u, s, e: True,
            send_notify=lambda url, text: sent.append(text) or True,
        ),
        checkpointer=MemorySaver(),
    )
    status = runner.process(
        {"type": "start", "tenant_id": TENANT, "workflow_run_id": "wfrun_ft"}
    )
    return status, store, sent


def test_発火トリガーの経路だけが実行される() -> None:
    status, store, sent = _run("t2")
    assert status == "succeeded"
    assert sent == ["経路B"]  # 経路A は実行されない
    assert ("wfrun_ft", "n2") in store.node_runs
    assert ("wfrun_ft", "n1") not in store.node_runs


def test_手動発火はmanual経路だけを実行する() -> None:
    status, _, sent = _run("t1")
    assert status == "succeeded"
    assert sent == ["経路A"]


def test_発火トリガー不明の旧runは従来どおり全経路() -> None:
    status, store, sent = _run(None)
    assert status == "succeeded"
    assert sorted(sent) == ["経路A", "経路B"]
    assert ("wfrun_ft", "n1") in store.node_runs
    assert ("wfrun_ft", "n2") in store.node_runs
