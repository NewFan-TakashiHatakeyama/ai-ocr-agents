"""workflow-runner の配線（§16 設計 v0.2 §6 / P3）。

InMemory store + MemorySaver で、セグメント実行・interrupt 分類・resume・冪等・
失敗と retry を検証する。実 PostgreSQL（lg_wf 分離・プロセス跨ぎ）は
test_workflow_runner_pg.py が担当。
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("langgraph.checkpoint.memory")

from langgraph.checkpoint.memory import MemorySaver  # noqa: E402

from newfan_orchestrator.consumer import InMemoryQueueConsumer  # noqa: E402
from newfan_orchestrator.workflow_graph import RunnerDeps  # noqa: E402
from newfan_orchestrator.workflow_runner import NotReady, WorkflowRunner  # noqa: E402
from newfan_orchestrator.workflow_store import InMemoryWorkflowRunStore  # noqa: E402

TENANT = "ten_1"

# P3 の代表グラフ: manual → extract → 分岐（needs_review → webhook / else → map → webhook）
GRAPH: dict[str, Any] = {
    "version": 1,
    "nodes": [
        {"id": "t1", "type": "source.manual", "config": {}},
        {"id": "x1", "type": "process.extract", "config": {"schema_id": "sch_inv"}},
        {
            "id": "b1",
            "type": "branch.condition",
            "config": {
                "branches": [{"when": "run.status == 'needs_review'", "to": "s1"}],
                "else": "m1",
            },
        },
        {
            "id": "m1",
            "type": "transform.map_fields",
            "config": {
                "mappings": [
                    {"from": "total_amount", "to": "amount"},
                    {"const": "ai-ocr", "to": "source_system"},
                ]
            },
        },
        {"id": "s1", "type": "sink.webhook", "config": {"connection_id": "con_hook"}},
    ],
    "edges": [
        {"from": "t1", "to": "x1"},
        {"from": "x1", "to": "b1"},
        {"from": "m1", "to": "s1"},
    ],
}


class FakeWebhook:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, dict[str, Any]]] = []
        self.fail_times = 0

    def send(self, url: str, secret: str, event: dict[str, Any]) -> bool:
        if self.fail_times > 0:
            self.fail_times -= 1
            return False
        self.sent.append((url, secret, event))
        return True


@pytest.fixture
def env() -> tuple[WorkflowRunner, InMemoryWorkflowRunStore, FakeWebhook, InMemoryQueueConsumer]:
    store = InMemoryWorkflowRunStore()
    store.seed_run("wfrun_1", tenant_id=TENANT, workflow_id="workflow_1", graph_json=GRAPH)
    store.seed_webhook(TENANT, "con_hook", "https://example.com/hook", "sec")
    webhook = FakeWebhook()
    consumer = InMemoryQueueConsumer()
    runner = WorkflowRunner(
        store,
        consumer,
        RunnerDeps(store=store, send_webhook=webhook.send),
        checkpointer=MemorySaver(),
    )
    return runner, store, webhook, consumer


def _start(runner: WorkflowRunner) -> str:
    return runner.process({"type": "start", "tenant_id": TENANT, "workflow_run_id": "wfrun_1"})


def _resume(runner: WorkflowRunner, status: str) -> str:
    return runner.process(
        {
            "type": "resume",
            "tenant_id": TENANT,
            "workflow_run_id": "wfrun_1",
            "event": {"kind": "extract_done", "run_id": "run_wf_1", "status": status},
        }
    )


def test_startで抽出をenqueueしてawait_extractで止まる(env) -> None:
    runner, store, webhook, _ = env
    assert _start(runner) == "running"
    # 抽出 Run が 1 件発行され、notify 先が q.workflow を指す
    assert len(store.enqueued) == 1
    assert store.enqueued[0]["notify"]["stream"] == "q.workflow"
    run = store.runs["wfrun_1"]
    assert run["status"] == "running"
    assert run["waiting"]["kind"] == "await_extract"
    # extract ノードは待機中（running）。webhook はまだ
    assert store.node_runs[("wfrun_1", "x1")]["status"] == "running"
    assert webhook.sent == []


def test_needs_reviewはwebhookへ直行しfields断面を届ける(env) -> None:
    runner, store, webhook, _ = env
    _start(runner)
    store.seed_extract_result(
        "run_wf_1",
        {
            "doc_type": "invoice",
            "fields": {"total_amount": {"value": "7003", "confidence": 1.0}},
            "run_confidence": 1.0,
        },
    )
    assert _resume(runner, "needs_review") == "succeeded"
    assert len(webhook.sent) == 1
    url, secret, event = webhook.sent[0]
    assert (url, secret) == ("https://example.com/hook", "sec")
    assert event["id"] == "wfrun_1:s1"  # 受信側 dedupe 用
    # map を経ない経路なので fields の値がそのまま
    assert event["data"] == {"total_amount": "7003"}
    assert store.runs["wfrun_1"]["status"] == "succeeded"
    assert store.node_runs[("wfrun_1", "x1")]["status"] == "succeeded"
    assert store.node_runs[("wfrun_1", "s1")]["status"] == "succeeded"


def test_confirmedはmapを通ってwebhookへ(env) -> None:
    runner, store, webhook, _ = env
    _start(runner)
    store.seed_extract_result(
        "run_wf_1",
        {"doc_type": "invoice", "fields": {"total_amount": {"value": "7003", "confidence": 1.0}}},
    )
    assert _resume(runner, "confirmed") == "succeeded"
    event = webhook.sent[0][2]
    # map_fields の出力（リネーム + 固定値）が届く
    assert event["data"] == {"amount": "7003", "source_system": "ai-ocr"}
    assert store.node_runs[("wfrun_1", "m1")]["status"] == "succeeded"


def test_別プロセス相当でもresumeできる(env) -> None:
    # runner を作り直す（compile キャッシュも state も持たない）。checkpoint だけが共有
    # ＝プロセス再起動の相当。実 PG 版は test_workflow_runner_pg.py。
    runner, store, webhook, consumer = env
    _start(runner)
    saver = runner._checkpointer  # noqa: SLF001 - 同一ストレージを共有するのが再起動の条件
    runner2 = WorkflowRunner(
        store,
        consumer,
        RunnerDeps(store=store, send_webhook=webhook.send),
        checkpointer=saver,
    )
    store.seed_extract_result(
        "run_wf_1", {"fields": {"total_amount": {"value": "1", "confidence": 1.0}}}
    )
    assert _resume(runner2, "needs_review") == "succeeded"
    assert len(webhook.sent) == 1


def test_extractのenqueueは再実行でも1回だけ(env) -> None:
    # interrupt より前のコードは resume で再実行される（実測済み）。
    # ensure_extract_run の冪等キーで「二重 Run」を防いでいることを固定する。
    runner, store, webhook, _ = env
    _start(runner)
    store.seed_extract_result(
        "run_wf_1", {"fields": {"total_amount": {"value": "1", "confidence": 1.0}}}
    )
    _resume(runner, "needs_review")
    assert len(store.enqueued) == 1  # resume の replay でも増えない
    assert len(store.extract_runs) == 1


def test_start重複は無害(env) -> None:
    runner, store, webhook, _ = env
    assert _start(runner) == "running"
    # ack 前クラッシュの再配信を模す。最初からやり直さず、射影だけ合う
    assert _start(runner) == "running"
    assert len(store.enqueued) == 1
    assert store.runs["wfrun_1"]["waiting"]["kind"] == "await_extract"


def test_checkpoint未確定のresumeはNotReady(env) -> None:
    # 抽出完了 notify が start の checkpoint より先に届くレース。ack せず再配信に委ねる
    runner, store, _, _ = env
    with pytest.raises(NotReady):
        _resume(runner, "needs_review")


def test_未知のrunはNotReady(env) -> None:
    runner, _, _, _ = env
    with pytest.raises(NotReady):
        runner.process({"type": "start", "tenant_id": TENANT, "workflow_run_id": "wfrun_ghost"})


def test_webhook失敗はfailedになりretryで完走する(env) -> None:
    runner, store, webhook, _ = env
    webhook.fail_times = 1
    _start(runner)
    store.seed_extract_result(
        "run_wf_1", {"fields": {"total_amount": {"value": "1", "confidence": 1.0}}}
    )
    assert _resume(runner, "needs_review") == "failed"
    assert store.runs["wfrun_1"]["status"] == "failed"
    assert store.node_runs[("wfrun_1", "s1")]["status"] == "failed"
    assert store.runs["wfrun_1"]["error"]["message"].startswith("webhook")

    # retry は checkpoint から続き（完了済みノードは再実行されない）
    assert runner.process(
        {"type": "retry", "tenant_id": TENANT, "workflow_run_id": "wfrun_1"}
    ) == "succeeded"
    assert len(webhook.sent) == 1
    assert store.node_runs[("wfrun_1", "s1")]["status"] == "succeeded"
    # 抽出は 1 回のまま（retry で再実行されていない）
    assert len(store.enqueued) == 1


def test_run_onceはNotReadyをackしない(env) -> None:
    runner, store, _, consumer = env
    consumer.push({"type": "resume", "tenant_id": TENANT, "workflow_run_id": "wfrun_1",
                   "event": {"kind": "extract_done", "run_id": "r", "status": "confirmed"}})
    assert runner.run_once() == 0
    assert consumer.pending_count() == 1  # 再配信待ちで残る

    consumer.push({"type": "start", "tenant_id": TENANT, "workflow_run_id": "wfrun_1"})
    assert runner.run_once() >= 1  # start は処理され、先の resume も後続で処理できる
