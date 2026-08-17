# mypy: ignore-errors
"""GDrive ポーリングトリガー（⑤⑥ SaaS連携）: FakeGDriveProvider + GDrivePoller。

常駐ゼロ方針（2026-08-17 ユーザー決定）: worker 主ループ内の interval ポーリング。
冪等は S3 トリガーと同一の source_cursors claim（DD-13）を使う。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from newfan_orchestrator.gdrive import FakeGDriveProvider
from newfan_orchestrator.workflow_trigger import GDrivePoller, InMemoryTriggerStore

TENANT = "ten_1"


@dataclass
class _Page:
    page_no: int = 1
    width: int = 100
    height: int = 100
    image_uri: str = "file:///tmp/p1.png"
    preproc: dict | None = None


@dataclass
class _IngestResult:
    storage_uri: str = "file:///tmp/original.png"
    mime_type: str = "image/png"
    page_count: int = 1
    pages: tuple = (_Page(),)


def _graph(connection_id: str = "con_gd", extensions: list[str] | None = None) -> dict[str, Any]:
    cfg: dict[str, Any] = {"connection_id": connection_id}
    if extensions is not None:
        cfg["extensions"] = extensions
    return {
        "version": 1,
        "nodes": [
            {"id": "t1", "type": "source.gdrive_event", "config": cfg},
            {"id": "x1", "type": "process.extract", "config": {"schema_id": "sch_inv"}},
        ],
        "edges": [{"from": "t1", "to": "x1"}],
    }


def _poller(store: InMemoryTriggerStore, root: str, **kw) -> tuple[GDrivePoller, list]:
    enqueued: list[dict[str, Any]] = []
    poller = GDrivePoller(
        store=store,
        provider=FakeGDriveProvider(root),
        ingest=lambda _u: _IngestResult(),
        enqueue=lambda s, m: enqueued.append({"stream": s, **m}),
        poll_interval_sec=0.0,
        **kw,
    )
    return poller, enqueued


def _seed(store: InMemoryTriggerStore, tmp_path, folder: str = "inbox") -> None:
    store.seed_workflow(TENANT, "wf_gd", 1, _graph())
    store.seed_gdrive_connection(TENANT, "con_gd", folder)
    (tmp_path / folder).mkdir(exist_ok=True)


def test_新着ファイルを検知して取込みワークフローを発火する(tmp_path) -> None:
    store = InMemoryTriggerStore()
    _seed(store, tmp_path)
    (tmp_path / "inbox" / "請求書_ABC.pdf").write_bytes(b"pdf-bytes")

    poller, enqueued = _poller(store, str(tmp_path))
    n = poller.poll_all()

    assert n == 1
    assert len(store.documents) == 1
    doc = store.documents[0]
    assert doc["original_name"] == "請求書_ABC.pdf"
    assert doc["external_ref"].startswith("gdrive://inbox/")
    assert len(enqueued) == 1 and enqueued[0]["stream"] == "q.workflow"
    assert enqueued[0]["type"] == "start"


def test_同じ内容の再検知はclaimでskipされる(tmp_path) -> None:
    store = InMemoryTriggerStore()
    _seed(store, tmp_path)
    (tmp_path / "inbox" / "a.pdf").write_bytes(b"same-bytes")

    poller, enqueued = _poller(store, str(tmp_path))
    assert poller.poll_all() == 1
    assert poller.poll_all() == 0  # 2回目は claim 済み → 取り込まない
    assert len(store.documents) == 1
    assert len(enqueued) == 1


def test_内容が変わったファイルは再取込される(tmp_path) -> None:
    store = InMemoryTriggerStore()
    _seed(store, tmp_path)
    f = tmp_path / "inbox" / "a.pdf"
    f.write_bytes(b"v1")

    poller, _ = _poller(store, str(tmp_path))
    assert poller.poll_all() == 1
    f.write_bytes(b"v2")  # Drive 上の「同じファイルの更新」相当（hash 変化）
    assert poller.poll_all() == 1
    assert len(store.documents) == 2


def test_拡張子フィルタに合わないファイルは無視する(tmp_path) -> None:
    store = InMemoryTriggerStore()
    store.seed_workflow(TENANT, "wf_gd", 1, _graph(extensions=[".pdf"]))
    store.seed_gdrive_connection(TENANT, "con_gd", "inbox")
    (tmp_path / "inbox").mkdir()
    (tmp_path / "inbox" / "memo.txt").write_bytes(b"not a doc")

    poller, enqueued = _poller(store, str(tmp_path))
    assert poller.poll_all() == 0
    assert store.documents == []
    assert enqueued == []


def test_接続が無ければskipして落ちない(tmp_path) -> None:
    store = InMemoryTriggerStore()
    store.seed_workflow(TENANT, "wf_gd", 1, _graph())  # 接続は seed しない
    poller, _ = _poller(store, str(tmp_path))
    assert poller.poll_all() == 0


def test_sync_nowは指定接続だけを即時同期する(tmp_path) -> None:
    store = InMemoryTriggerStore()
    _seed(store, tmp_path)
    (tmp_path / "inbox" / "b.png").write_bytes(b"png-bytes")

    poller, enqueued = _poller(store, str(tmp_path))
    n = poller.sync_now(TENANT, "con_gd")
    assert n == 1
    assert len(enqueued) == 1


def test_run_onceはinterval内の再実行を間引く(tmp_path) -> None:
    store = InMemoryTriggerStore()
    _seed(store, tmp_path)
    (tmp_path / "inbox" / "c.pdf").write_bytes(b"c")
    enqueued: list = []
    poller = GDrivePoller(
        store=store,
        provider=FakeGDriveProvider(str(tmp_path)),
        ingest=lambda _u: _IngestResult(),
        enqueue=lambda s, m: enqueued.append(m),
        poll_interval_sec=3600.0,  # 長い interval
    )
    assert poller.run_once() == 1
    (tmp_path / "inbox" / "d.pdf").write_bytes(b"d")
    assert poller.run_once() == 0  # interval 内は間引かれる（次の tick で拾う）
