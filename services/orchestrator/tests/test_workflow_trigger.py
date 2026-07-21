"""S3 イベント駆動トリガー（§16 設計 v0.2 §7.2 / P4）。

契約の要点を固定する:
- キー規約: 最上位フォルダ = テナント ID。prefix/extensions はテナントフォルダを
  除いた相対キーに対して照合する（config に tenant_id を書かせない）
- DD-13: 同一 (connection, key, ETag) は skip、同じキーでも ETag が変われば再処理
- 失敗したメッセージは delete しない（SQS 再配信 → DLQ に委ねる）
- ポーリングは間隔ゲート（毎ループ SQS を叩かない）
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from newfan_orchestrator.workflow_trigger import (
    InMemoryTriggerStore,
    S3TriggerConsumer,
    match_s3_event,
)

GRAPH = {
    "version": 1,
    "nodes": [
        {
            "id": "t1",
            "type": "source.s3_event",
            "config": {"connection_id": "con_s3", "prefix": "invoices/"},
        },
        {"id": "x1", "type": "process.extract", "config": {"schema_id": "sch_inv"}},
    ],
    "edges": [{"from": "t1", "to": "x1"}],
}


def _event(key: str, etag: str = "etag1", bucket: str = "inbox-bkt") -> dict[str, Any]:
    # EventBridge の "Object Created" イベント形（detail に bucket/object）
    return {
        "detail-type": "Object Created",
        "detail": {"bucket": {"name": bucket}, "object": {"key": key, "etag": etag}},
    }


class FakeSqs:
    """receive/delete だけの boto3 互換。queue は _process 対象の Body リスト。"""

    def __init__(self, bodies: list[dict[str, Any]]) -> None:
        self.pending = [
            {"Body": json.dumps(b), "ReceiptHandle": f"rh{i}"} for i, b in enumerate(bodies)
        ]
        self.deleted: list[str] = []
        self.receive_calls = 0

    def receive_message(self, **kw: Any) -> dict[str, Any]:
        self.receive_calls += 1
        msgs, self.pending = self.pending, []
        return {"Messages": msgs}

    def delete_message(self, *, QueueUrl: str, ReceiptHandle: str) -> None:  # noqa: N803
        self.deleted.append(ReceiptHandle)


@dataclass
class FakeIngestResult:
    storage_uri: str = "s3://main/ten_1/doc/original.png"
    mime_type: str = "image/png"
    page_count: int = 1
    pages: list[Any] = field(default_factory=list)


class _Page:
    page_no = 1
    width = 100
    height = 200
    image_uri = "s3://main/ten_1/doc/p1.png"
    preproc = {"deskew": 0.0}


def _consumer(
    store: InMemoryTriggerStore,
    sqs: FakeSqs,
    *,
    ingest: Any = None,
    interval: float = 0.0,
) -> tuple[S3TriggerConsumer, list[dict[str, Any]], list[Any]]:
    enqueued: list[dict[str, Any]] = []
    fetched: list[tuple[str, str]] = []
    ingested: list[Any] = []

    def _ingest(inp: Any) -> FakeIngestResult:
        ingested.append(inp)
        return FakeIngestResult(pages=[_Page()])

    c = S3TriggerConsumer(
        sqs=sqs,
        queue_url="q",
        store=store,
        fetch=lambda b, k: fetched.append((b, k)) or b"png-bytes",
        ingest=ingest or _ingest,
        enqueue=lambda stream, msg: enqueued.append({"stream": stream, **msg}),
        poll_interval_sec=interval,
    )
    return c, enqueued, ingested


def _store() -> InMemoryTriggerStore:
    s = InMemoryTriggerStore()
    s.seed_workflow("ten_1", "wf_1", 3, GRAPH)
    s.seed_s3_connection("ten_1", "con_s3", "inbox-bkt")
    return s


# ---------- match_s3_event（純関数） ----------


def test_matchはprefixとextensionsを相対キーで照合する() -> None:
    store = _store()
    bucket_of = lambda cid: store.get_s3_connection_bucket("ten_1", cid)  # noqa: E731
    wfs = store.list_active_workflows("ten_1")

    assert len(match_s3_event(wfs, bucket_of, "inbox-bkt", "invoices/a.PDF")) == 1
    # prefix 不一致 / 拡張子不一致 / バケット不一致はどれも発火しない
    assert match_s3_event(wfs, bucket_of, "inbox-bkt", "receipts/a.pdf") == []
    assert match_s3_event(wfs, bucket_of, "inbox-bkt", "invoices/a.zip") == []
    assert match_s3_event(wfs, bucket_of, "other-bkt", "invoices/a.pdf") == []


def test_matchは壊れたgraphを除外して他を生かす() -> None:
    store = _store()
    store.seed_workflow("ten_1", "wf_broken", 1, {"nodes": "broken"})
    matches = match_s3_event(
        store.list_active_workflows("ten_1"),
        lambda cid: store.get_s3_connection_bucket("ten_1", cid),
        "inbox-bkt",
        "invoices/a.pdf",
    )
    assert [m.workflow_id for m in matches] == ["wf_1"]


# ---------- consumer 本体 ----------


def test_一致イベントはingestしてrunを起こしstartを積む() -> None:
    store = _store()
    sqs = FakeSqs([_event("ten_1/invoices/a.png")])
    c, enqueued, ingested = _consumer(store, sqs)

    assert c.run_once() == 1
    assert sqs.deleted == ["rh0"]
    # ingest には元キー由来のファイル名と external_ref が渡る
    assert ingested[0].tenant_id == "ten_1"
    assert ingested[0].filename == "a.png"
    assert ingested[0].external_ref == "s3://inbox-bkt/ten_1/invoices/a.png"
    # document + run が同時に登録され、start が run 毎に積まれる
    assert store.documents[0]["external_ref"] == "s3://inbox-bkt/ten_1/invoices/a.png"
    assert len(store.runs) == 1
    assert store.runs[0]["workflow_id"] == "wf_1"
    assert enqueued == [
        {
            "stream": "q.workflow",
            "type": "start",
            "tenant_id": "ten_1",
            "workflow_run_id": store.runs[0]["id"],
        }
    ]


def test_テナント規約外のキーはskipして削除する() -> None:
    store = _store()
    sqs = FakeSqs([_event("toplevel.png"), _event("ten_1/")])
    c, enqueued, _ = _consumer(store, sqs)
    assert c.run_once() == 2  # 処理済み（=削除）として数える。毒メッセージ化させない
    assert len(sqs.deleted) == 2
    assert store.runs == [] and enqueued == []


def test_一致しないイベントは削除だけする() -> None:
    store = _store()
    sqs = FakeSqs([_event("ten_1/receipts/a.png")])
    c, _, ingested = _consumer(store, sqs)
    assert c.run_once() == 1
    assert sqs.deleted == ["rh0"]
    assert ingested == [] and store.documents == []


def test_同一ETagの再配置はingestせずskipする() -> None:
    store = _store()
    sqs = FakeSqs([_event("ten_1/invoices/a.png", etag="e1")])
    c, _, ingested = _consumer(store, sqs)
    c.run_once()
    assert len(store.runs) == 1

    sqs2 = FakeSqs([_event("ten_1/invoices/a.png", etag="e1")])
    c2, enqueued2, ingested2 = _consumer(store, sqs2)
    assert c2.run_once() == 1  # skip も正常処理（削除する）
    assert sqs2.deleted == ["rh0"]
    assert ingested2 == [] and len(store.runs) == 1 and enqueued2 == []


def test_ETagが変わった同一キーは再処理する() -> None:
    store = _store()
    c, _, _ = _consumer(store, FakeSqs([_event("ten_1/invoices/a.png", etag="e1")]))
    c.run_once()
    c2, _, ingested2 = _consumer(store, FakeSqs([_event("ten_1/invoices/a.png", etag="e2")]))
    c2.run_once()
    # 中身が違う（差し替え）なら別帳票として取り込む
    assert len(ingested2) == 1
    assert len(store.runs) == 2


def test_処理失敗はdeleteせず後続は処理する() -> None:
    store = _store()
    sqs = FakeSqs(
        [_event("ten_1/invoices/bad.png"), _event("ten_1/invoices/good.png")]
    )

    def _flaky(inp: Any) -> FakeIngestResult:
        if inp.filename == "bad.png":
            raise RuntimeError("S3 ingest failed")
        return FakeIngestResult(pages=[_Page()])

    c, enqueued, _ = _consumer(store, sqs, ingest=_flaky)
    assert c.run_once() == 1
    # bad は削除されない → visibility timeout 後に再配信され、繰り返せば DLQ へ
    assert sqs.deleted == ["rh1"]
    assert [r["source_key"] for r in store.runs] == ["ten_1/invoices/good.png"]
    assert len(enqueued) == 1


def test_ポーリングは間隔ゲートで間引かれる() -> None:
    store = _store()
    sqs = FakeSqs([])
    c, _, _ = _consumer(store, sqs, interval=60.0)
    c.run_once()
    c.run_once()
    c.run_once()
    # 初回だけ SQS を叩き、間隔内の呼び出しは API を呼ばない（空ポーリング課金対策）
    assert sqs.receive_calls == 1


def test_複数ワークフローが一致すればrunは複数起きdocumentは1つ() -> None:
    store = _store()
    graph2 = json.loads(json.dumps(GRAPH))
    graph2["nodes"][0]["config"]["prefix"] = "invoices/"
    store.seed_workflow("ten_1", "wf_2", 1, graph2)
    c, enqueued, ingested = _consumer(store, FakeSqs([_event("ten_1/invoices/a.png")]))
    c.run_once()
    assert len(ingested) == 1 and len(store.documents) == 1
    assert {r["workflow_id"] for r in store.runs} == {"wf_1", "wf_2"}
    assert len(enqueued) == 2


def test_S3イベントでないBodyはskipして削除する() -> None:
    store = _store()
    sqs = FakeSqs([{"detail-type": "Scheduled Event", "detail": {}}])
    c, _, _ = _consumer(store, sqs)
    assert c.run_once() == 1
    assert len(sqs.deleted) == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))


def test_activeワークフローゼロなら削除せず保留する() -> None:
    # down（DB destroy）→ up 直後の状態。定義を復元する前に滞留イベントを消化すると
    # 全部失われるため、削除せず SQS に残す（→5 回で DLQ → redrive で復帰）。
    store = InMemoryTriggerStore()
    store.seed_s3_connection("ten_1", "con_s3", "inbox-bkt")  # 接続はあるが workflow ゼロ
    sqs = FakeSqs([_event("ten_1/invoices/a.png")])
    c, enqueued, ingested = _consumer(store, sqs)
    assert c.run_once() == 0
    assert sqs.deleted == []  # 削除しない
    assert ingested == [] and enqueued == []

    # 定義が復元されたら、同じイベントの再配信で普通に処理される
    store.seed_workflow("ten_1", "wf_1", 1, GRAPH)
    sqs2 = FakeSqs([_event("ten_1/invoices/a.png")])
    c2, enqueued2, _ = _consumer(store, sqs2)
    assert c2.run_once() == 1
    assert len(enqueued2) == 1
