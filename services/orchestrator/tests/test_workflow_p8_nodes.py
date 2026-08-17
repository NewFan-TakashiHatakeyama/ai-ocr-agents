"""P8 ノード（classify / file / notify）と ScheduleTicker の配線（§16 P8）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

import pytest

pytest.importorskip("langgraph.checkpoint.memory")

from langgraph.checkpoint.memory import MemorySaver  # noqa: E402

from newfan_orchestrator.consumer import InMemoryQueueConsumer  # noqa: E402
from newfan_orchestrator.workflow_graph import RunnerDeps  # noqa: E402
from newfan_orchestrator.workflow_runner import WorkflowRunner  # noqa: E402
from newfan_orchestrator.workflow_store import InMemoryWorkflowRunStore  # noqa: E402
from newfan_orchestrator.workflow_trigger import (  # noqa: E402
    InMemoryTriggerStore,
    ScheduleTicker,
)

TENANT = "ten_1"


def _graph(nodes: list[dict[str, Any]], edges: list[dict[str, str]]) -> dict[str, Any]:
    return {"version": 1, "nodes": nodes, "edges": edges}


def _extract_nodes() -> list[dict[str, Any]]:
    return [
        {"id": "t1", "type": "source.manual", "config": {}},
        {"id": "x1", "type": "process.extract", "config": {"schema_id": "sch_inv"}},
    ]


class Sinks:
    def __init__(self) -> None:
        self.files: list[tuple[str, str, bytes, str]] = []
        self.notifies: list[tuple[str, str]] = []
        self.notify_ok = True

    def write_file(self, bucket: str, key: str, body: bytes, ctype: str) -> None:
        self.files.append((bucket, key, body, ctype))

    def send_notify(self, url: str, text: str) -> bool:
        self.notifies.append((url, text))
        return self.notify_ok


def _run(graph: dict[str, Any], *, fields: Optional[dict[str, Any]] = None,
         doc_type: str = "invoice",
         original_name: Optional[str] = None) -> tuple[str, InMemoryWorkflowRunStore, Sinks]:
    store = InMemoryWorkflowRunStore()
    store.seed_run("wfrun_p8", tenant_id=TENANT, workflow_id="workflow_p8", graph_json=graph)
    store.seed_webhook(TENANT, "con_slack", "https://hooks.example/slack", "")
    store.seed_s3_connection(TENANT, "con_s3out", "out-bucket")
    sinks = Sinks()
    runner = WorkflowRunner(
        store, InMemoryQueueConsumer(),
        RunnerDeps(
            store=store, send_webhook=lambda u, s, e: True,
            write_file=sinks.write_file, send_notify=sinks.send_notify,
        ),
        checkpointer=MemorySaver(),
    )
    runner.process({"type": "start", "tenant_id": TENANT, "workflow_run_id": "wfrun_p8"})
    result = {
        "doc_type": doc_type,
        "fields": fields
        or {"invoice_no": {"value": "GS0001", "confidence": 1.0},
            "total_amount": {"value": "7003", "confidence": 1.0}},
    }
    if original_name is not None:
        result["original_name"] = original_name
    store.seed_extract_result("run_wf_1", result)
    status = runner.process({
        "type": "resume", "tenant_id": TENANT, "workflow_run_id": "wfrun_p8",
        "event": {"kind": "extract_done", "run_id": "run_wf_1", "status": "confirmed"},
    })
    return status, store, sinks


# ---------- process.classify ----------


def test_classifyは対象doc_typeなら素通りする() -> None:
    g = _graph(
        [*_extract_nodes(),
         {"id": "c1", "type": "process.classify",
          "config": {"doc_types": ["invoice", "receipt"]}}],
        [{"from": "t1", "to": "x1"}, {"from": "x1", "to": "c1"}],
    )
    status, store, _ = _run(g)
    assert status == "succeeded"
    assert store.node_runs[("wfrun_p8", "c1")]["output"] == {
        "doc_type": "invoice", "known": True,
    }


def test_classifyのon_unknown_haltは対象外で止める() -> None:
    g = _graph(
        [*_extract_nodes(),
         {"id": "c1", "type": "process.classify",
          "config": {"doc_types": ["receipt"], "on_unknown": "halt"}}],
        [{"from": "t1", "to": "x1"}, {"from": "x1", "to": "c1"}],
    )
    status, store, _ = _run(g)
    assert status == "failed"
    assert "分類対象外" in store.runs["wfrun_p8"]["error"]["message"]


def test_classifyのdefault_routeは対象外でも継続する() -> None:
    g = _graph(
        [*_extract_nodes(),
         {"id": "c1", "type": "process.classify", "config": {"doc_types": ["receipt"]}}],
        [{"from": "t1", "to": "x1"}, {"from": "x1", "to": "c1"}],
    )
    status, store, _ = _run(g)
    assert status == "succeeded"
    assert store.node_runs[("wfrun_p8", "c1")]["output"]["known"] is False


def test_classifyは内容がスキーマと食い違えば取り違えを止める() -> None:
    # スキーマは invoice を返すが、ファイル名は発注書 → 内容分類が purchase_order。
    # 許可リストは invoice のみなので halt する（請求ワークフローへの発注書混入検知）。
    g = _graph(
        [*_extract_nodes(),
         {"id": "c1", "type": "process.classify",
          "config": {"doc_types": ["invoice"], "on_unknown": "halt"}}],
        [{"from": "t1", "to": "x1"}, {"from": "x1", "to": "c1"}],
    )
    status, store, _ = _run(g, doc_type="invoice", original_name="発注書_ABC商事_0001.pdf")
    assert status == "failed"
    assert "分類対象外" in store.runs["wfrun_p8"]["error"]["message"]


def test_classifyは内容が許可種別と一致すれば通す() -> None:
    # ファイル名が請求書 → 内容分類 invoice。許可リストにあるので通す＋観測値を残す。
    g = _graph(
        [*_extract_nodes(),
         {"id": "c1", "type": "process.classify",
          "config": {"doc_types": ["invoice"], "on_unknown": "halt"}}],
        [{"from": "t1", "to": "x1"}, {"from": "x1", "to": "c1"}],
    )
    status, store, _ = _run(g, doc_type="invoice", original_name="請求書_ABC_2026.pdf")
    assert status == "succeeded"
    out = store.node_runs[("wfrun_p8", "c1")]["output"]
    assert out["known"] is True
    assert out["content_doc_type"] == "invoice"


# ---------- sink.file ----------


def test_fileはmapの断面をJSONでS3へ書きパスを展開する() -> None:
    g = _graph(
        [*_extract_nodes(),
         {"id": "m1", "type": "transform.map_fields",
          "config": {"mappings": [{"from": "total_amount", "to": "amount"}]}},
         {"id": "f1", "type": "sink.file",
          "config": {"connection_id": "con_s3out",
                     "path": "exports/{workflow_run_id}/{node_id}.json"}}],
        [{"from": "t1", "to": "x1"}, {"from": "x1", "to": "m1"}, {"from": "m1", "to": "f1"}],
    )
    status, store, sinks = _run(g)
    assert status == "succeeded"
    bucket, key, body, ctype = sinks.files[0]
    assert (bucket, key, ctype) == ("out-bucket", "exports/wfrun_p8/f1.json", "application/json")
    assert b'"amount": "7003"' in body
    assert store.node_runs[("wfrun_p8", "f1")]["output"]["key"] == "exports/wfrun_p8/f1.json"


def test_fileのcsvはBOM付きヘッダ行になる() -> None:
    g = _graph(
        [*_extract_nodes(),
         {"id": "f1", "type": "sink.file",
          "config": {"connection_id": "con_s3out", "path": "x.csv", "format": "csv"}}],
        [{"from": "t1", "to": "x1"}, {"from": "x1", "to": "f1"}],
    )
    status, _, sinks = _run(g)
    assert status == "succeeded"
    _, _, body, ctype = sinks.files[0]
    assert ctype == "text/csv"
    assert body.startswith(b"\xef\xbb\xbf")  # Excel 互換 BOM
    assert b"invoice_no,total_amount" in body


def test_fileはs3接続が無ければ失敗する() -> None:
    g = _graph(
        [*_extract_nodes(),
         {"id": "f1", "type": "sink.file", "config": {"connection_id": "con_nai", "path": "x"}}],
        [{"from": "t1", "to": "x1"}, {"from": "x1", "to": "f1"}],
    )
    status, store, sinks = _run(g)
    assert status == "failed" and sinks.files == []


# ---------- sink.notify ----------


def test_notifyはテンプレートを展開してSlack互換で送る() -> None:
    g = _graph(
        [*_extract_nodes(),
         {"id": "n1", "type": "sink.notify",
          "config": {"connection_id": "con_slack",
                     "template": "帳票 {document_id} を処理しました（合計 {total_amount} 円）"}}],
        [{"from": "t1", "to": "x1"}, {"from": "x1", "to": "n1"}],
    )
    status, _, sinks = _run(g)
    assert status == "succeeded"
    url, text = sinks.notifies[0]
    assert url == "https://hooks.example/slack"
    assert text == "帳票 doc_1 を処理しました（合計 7003 円）"


def test_notifyのwhen式が偽なら送らない() -> None:
    g = _graph(
        [*_extract_nodes(),
         {"id": "n1", "type": "sink.notify",
          "config": {"connection_id": "con_slack", "template": "x",
                     "when": "run.status == 'needs_review'"}}],
        [{"from": "t1", "to": "x1"}, {"from": "x1", "to": "n1"}],
    )
    status, store, sinks = _run(g)
    assert status == "succeeded"
    assert sinks.notifies == []
    assert store.node_runs[("wfrun_p8", "n1")]["output"]["sent"] is False


def test_notifyの送信失敗はrunをfailedにする() -> None:
    g = _graph(
        [*_extract_nodes(),
         {"id": "n1", "type": "sink.notify",
          "config": {"connection_id": "con_slack", "template": "x"}}],
        [{"from": "t1", "to": "x1"}, {"from": "x1", "to": "n1"}],
    )
    store = InMemoryWorkflowRunStore()
    store.seed_run("wfrun_p8", tenant_id=TENANT, workflow_id="w", graph_json=g)
    store.seed_webhook(TENANT, "con_slack", "https://hooks.example/slack", "")
    sinks = Sinks()
    sinks.notify_ok = False
    runner = WorkflowRunner(
        store, InMemoryQueueConsumer(),
        RunnerDeps(store=store, send_webhook=lambda u, s, e: True,
                   send_notify=sinks.send_notify),
        checkpointer=MemorySaver(),
    )
    runner.process({"type": "start", "tenant_id": TENANT, "workflow_run_id": "wfrun_p8"})
    store.seed_extract_result("run_wf_1", {"fields": {}})
    status = runner.process({
        "type": "resume", "tenant_id": TENANT, "workflow_run_id": "wfrun_p8",
        "event": {"kind": "extract_done", "run_id": "run_wf_1", "status": "confirmed"},
    })
    assert status == "failed"


# ---------- ScheduleTicker ----------

SCHED_GRAPH = _graph(
    [{"id": "t1", "type": "source.schedule", "config": {"cron": "0 9 * * *"}},
     {"id": "n1", "type": "sink.notify",
      "config": {"connection_id": "con_slack", "template": "morning"}}],
    [{"from": "t1", "to": "n1"}],
)


def _jst(h: int, m: int) -> datetime:
    from newfan_workflow.cron import JST

    return datetime(2026, 7, 21, h, m, 30, tzinfo=JST)


def test_tickerはcron一致の分にrunを1回だけ発火する() -> None:
    store = InMemoryTriggerStore()
    store.seed_workflow(TENANT, "wf_s", 1, SCHED_GRAPH)
    fired: list[dict[str, Any]] = []
    ticker = ScheduleTicker(store=store, enqueue=lambda s, m: fired.append(m))

    assert ticker.run_once(now=_jst(8, 59)) == 0
    assert ticker.run_once(now=_jst(9, 0)) == 1  # JST 9:00 → 発火
    assert fired[0]["type"] == "start" and fired[0]["tenant_id"] == TENANT
    assert store.runs[0]["document_id"] is None
    # 同じ分の再評価・別 consumer 相当は dedup される
    ticker2 = ScheduleTicker(store=store, enqueue=lambda s, m: fired.append(m))
    assert ticker2.run_once(now=_jst(9, 0)) == 0
    assert ticker.run_once(now=_jst(9, 1)) == 0  # 次の分は不一致


def test_tickerは短い停滞なら取りこぼさない() -> None:
    store = InMemoryTriggerStore()
    store.seed_workflow(TENANT, "wf_s", 1, SCHED_GRAPH)
    ticker = ScheduleTicker(store=store, enqueue=lambda s, m: None)
    ticker.run_once(now=_jst(8, 58))
    # ループが 3 分詰まっても 9:00 の発火は拾う（LOOKBACK 内）
    assert ticker.run_once(now=_jst(9, 2)) == 1


def test_tickerは長時間downの過去分は発火しない() -> None:
    store = InMemoryTriggerStore()
    store.seed_workflow(TENANT, "wf_s", 1, SCHED_GRAPH)
    ticker = ScheduleTicker(store=store, enqueue=lambda s, m: None)
    ticker.run_once(now=_jst(8, 0))
    # 2 時間後に再開（down 相当）。過ぎた 9:00 は遡らない（§1.3 即時性はスコープ外）
    assert ticker.run_once(now=_jst(11, 0)) == 0
