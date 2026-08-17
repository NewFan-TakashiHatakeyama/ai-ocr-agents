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
    # 監査で出所を誤認しないよう trigger 種別は gdrive_event（s3_event 固定だった回帰）
    assert store.runs[0]["trigger_type"] == "gdrive_event"


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


def test_存在しないフォルダはtestedに昇格しない(tmp_path) -> None:
    # folder_id タイポでも [] が返ると「疎通OK」扱いで tested に昇格し、検知ゼロのまま
    # 「テスト済」表示になるサイレント故障（レビュー確定）。Fake は不存在で例外を投げる
    store = InMemoryTriggerStore()
    store.seed_workflow(TENANT, "wf_gd", 1, _graph())
    store.seed_gdrive_connection(TENANT, "con_gd", "typo-folder")  # 実体を作らない
    poller, _ = _poller(store, str(tmp_path))
    assert poller.poll_all() == 0  # _poll_tenant が接続単位で例外を握って継続
    assert (TENANT, "con_gd") not in getattr(store, "tested_connections", set())


def test_1回のポーリングは件数上限で打ち切り残りは次回(tmp_path) -> None:
    # 無制限だと大量バックログで worker 主ループが長時間停止し、全テナントの抽出停止と
    # schedule 発火の恒久喪失（LOOKBACK 5分超）を招く（レビュー確定major）
    store = InMemoryTriggerStore()
    _seed(store, tmp_path)
    for i in range(12):
        (tmp_path / "inbox" / f"doc_{i:02d}.pdf").write_bytes(f"body-{i}".encode())

    poller, enqueued = _poller(store, str(tmp_path))
    assert poller.poll_all() == 10  # MAX_FILES_PER_POLL
    assert poller.poll_all() == 2  # 続きは次の tick（claim 済みは飛ばす）
    assert len(store.documents) == 12
    assert len(enqueued) == 12


def test_同期成功で接続がtestedへ昇格する(tmp_path) -> None:
    # gdrive の疎通テストは gateway からできない（プロバイダは worker 側）ため、
    # フォルダ一覧の取得成功をもって untested→tested に昇格させる（lint L010 対応）
    store = InMemoryTriggerStore()
    _seed(store, tmp_path)
    poller, _ = _poller(store, str(tmp_path))
    poller.sync_now(TENANT, "con_gd")  # ファイルが無くても一覧成功＝疎通OK
    assert (TENANT, "con_gd") in store.tested_connections


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


# ---- ⑤⑥ 横展開: m365 / box（FolderEventPoller の種別パラメタ化） ----


def _folder_graph(node_type: str, connection_id: str = "con_f") -> dict:
    return {
        "version": 1,
        "nodes": [
            {"id": "t1", "type": node_type, "config": {"connection_id": connection_id}},
            {"id": "x1", "type": "process.extract", "config": {"schema_id": "sch_inv"}},
        ],
        "edges": [{"from": "t1", "to": "x1"}],
    }


def _typed_poller(store, root: str, conn_type: str, node_cls):
    from newfan_orchestrator.workflow_trigger import FolderEventPoller

    enqueued: list = []
    poller = FolderEventPoller(
        store=store,
        provider=FakeGDriveProvider(root),
        ingest=lambda _u: _IngestResult(),
        enqueue=lambda s, m: enqueued.append({"stream": s, **m}),
        poll_interval_sec=0.0,
        conn_type=conn_type,
        node_cls=node_cls,
    )
    return poller, enqueued


def test_m365ポーラーは同型に検知しtrigger種別を記録する(tmp_path) -> None:
    from newfan_workflow.models import M365EventNode

    store = InMemoryTriggerStore()
    store.seed_workflow(TENANT, "wf_m365", 1, _folder_graph("source.m365_event"))
    store.seed_folder_connection(TENANT, "con_f", "m365", "inbox")
    (tmp_path / "inbox").mkdir()
    (tmp_path / "inbox" / "発注書_XYZ.pdf").write_bytes(b"pdf")

    poller, enqueued = _typed_poller(store, str(tmp_path), "m365", M365EventNode)
    assert poller.poll_all() == 1
    assert store.documents[0]["external_ref"].startswith("m365://inbox/")
    assert store.runs[0]["trigger_type"] == "m365_event"
    assert store.runs[0]["source_key"].startswith("m365:")
    assert len(enqueued) == 1


def test_boxポーラーは同型に検知する(tmp_path) -> None:
    from newfan_workflow.models import BoxEventNode

    store = InMemoryTriggerStore()
    store.seed_workflow(TENANT, "wf_box", 1, _folder_graph("source.box_event"))
    store.seed_folder_connection(TENANT, "con_f", "box", "inbox")
    (tmp_path / "inbox").mkdir()
    (tmp_path / "inbox" / "領収書_01.png").write_bytes(b"png")

    poller, enqueued = _typed_poller(store, str(tmp_path), "box", BoxEventNode)
    assert poller.poll_all() == 1
    assert store.runs[0]["trigger_type"] == "box_event"
    assert len(enqueued) == 1


def test_種別が違う接続はポーラーから見えない(tmp_path) -> None:
    # gdrive ノードが box 接続を指しても（設定ミス）、型不一致で skip され誤取込しない
    from newfan_workflow.models import GDriveEventNode

    store = InMemoryTriggerStore()
    store.seed_workflow(TENANT, "wf_gd", 1, _folder_graph("source.gdrive_event"))
    store.seed_folder_connection(TENANT, "con_f", "box", "inbox")  # 型が違う
    (tmp_path / "inbox").mkdir()
    (tmp_path / "inbox" / "a.pdf").write_bytes(b"pdf")

    poller, enqueued = _typed_poller(store, str(tmp_path), "gdrive", GDriveEventNode)
    assert poller.poll_all() == 0
    assert enqueued == []


def test_gdriveノードはm365ポーラーにmatchしない(tmp_path) -> None:
    # 同一テナントに gdrive WF だけがある時、m365 ポーラーが誤って拾わない
    from newfan_workflow.models import M365EventNode

    store = InMemoryTriggerStore()
    store.seed_workflow(TENANT, "wf_gd", 1, _folder_graph("source.gdrive_event"))
    store.seed_folder_connection(TENANT, "con_f", "m365", "inbox")
    (tmp_path / "inbox").mkdir()
    (tmp_path / "inbox" / "a.pdf").write_bytes(b"pdf")

    poller, enqueued = _typed_poller(store, str(tmp_path), "m365", M365EventNode)
    assert poller.poll_all() == 0
    assert enqueued == []
