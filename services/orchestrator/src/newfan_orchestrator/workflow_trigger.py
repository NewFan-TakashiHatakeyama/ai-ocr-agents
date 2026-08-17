# mypy: ignore-errors
"""S3 イベント駆動トリガー（§16 設計 v0.2 §7.2 / P4）。

S3(inbox) → EventBridge → SQS（保持 14 日）→ 本 consumer（orchestrator-worker 同居）。
環境停止中のイベントは SQS に溜まり、up すると自動で drain される。常駐は増やさない。

キー規約: inbox バケットの最上位フォルダ = テナント ID（"{tenant_id}/…"）。
イベントにはテナント情報が無く、RLS 下の consumer はテナント横断検索ができないため、
キーからテナントを決めて RLS 文脈を張る。規約外のキーは記録して skip する。

冪等（DD-13）: (tenant, connection, source_key, ETag) を source_cursors の UNIQUE で
claim する。同一内容の再配置は skip、同じキーに中身違い（ETag 変化）は再処理される。
claim・documents・workflow_runs の登録は**同一トランザクション**（workflow_store 側）。
ingest（前処理画像の S3 書込み）はその前に行い、SQS 再配信時の書き直しは
同一キーへの上書きで無害。start の enqueue はコミット後（コミット前に積むと
runner が行を読めない瞬間ができる。読めなくても NotReady → 再配信で回復する）。
"""

from __future__ import annotations

import json
import logging
import posixpath
import time
import uuid
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol

from newfan_metrics import watch_lag_seconds
from newfan_workflow import WorkflowGraph
from newfan_workflow.cron import JST, CronError, cron_matches
from newfan_workflow.models import GDriveEventNode, S3EventNode, ScheduleNode

from newfan_orchestrator.gdrive import GDriveProvider

logger = logging.getLogger(__name__)

# SQS の空ポーリングにも API 課金があるため、毎ループではなく間隔を置いて叩く。
# 稼働中の検知遅延はこの間隔＋処理時間（数秒）。
POLL_INTERVAL_SEC = 5.0


@dataclass(frozen=True)
class TriggerMatch:
    workflow_id: str
    workflow_version: int
    graph_json: dict[str, Any]
    node_id: str
    connection_id: str


@dataclass(frozen=True)
class GDriveConnection:
    """type=gdrive の接続。folder_id は config、リフレッシュトークンは secret_ref。"""

    folder_id: str
    secret_ref: Optional[str]


class TriggerStore(Protocol):
    def list_active_workflows(self, tenant_id: str) -> list[tuple[str, int, dict[str, Any]]]: ...
    def get_s3_connection_bucket(self, tenant_id: str, connection_id: str) -> Optional[str]: ...
    def get_gdrive_connection(
        self, tenant_id: str, connection_id: str
    ) -> Optional[GDriveConnection]: ...
    def mark_connection_tested(self, tenant_id: str, connection_id: str) -> None: ...
    def already_claimed(
        self, tenant_id: str, connection_id: str, source_key: str, content_hash: str
    ) -> bool: ...
    def register_ingested(
        self,
        tenant_id: str,
        *,
        source_key: str,
        content_hash: str,
        document: dict[str, Any],
        pages: list[dict[str, Any]],
        matches: list[TriggerMatch],
    ) -> list[str]: ...
    def list_tenant_ids(self) -> list[str]: ...
    def register_scheduled_run(
        self,
        tenant_id: str,
        *,
        workflow_id: str,
        workflow_version: int,
        graph_json: dict[str, Any],
        node_id: str,
        fire_minute: str,
    ) -> Optional[str]: ...


def match_s3_event(
    workflows: list[tuple[str, int, dict[str, Any]]],
    bucket_of: Callable[[str], Optional[str]],
    bucket: str,
    key: str,
) -> list[TriggerMatch]:
    """active ワークフローの s3_event ノードとイベントを突き合わせる（純関数）。

    key はテナントフォルダを除いた相対キー（"invoices/a.pdf"）。config の prefix に
    自分の tenant_id を書かせないため（テナントは自分のフォルダ名を意識しない）。
    """
    out: list[TriggerMatch] = []
    for wf_id, version, graph_json in workflows:
        try:
            wf = WorkflowGraph.model_validate(graph_json)
        except Exception:  # noqa: BLE001 - 壊れた定義でトリガー全体を止めない
            logger.warning("[trigger] graph_json が読めないため除外: workflow=%s", wf_id)
            continue
        for node in wf.nodes:
            if not isinstance(node, S3EventNode):
                continue
            cfg = node.config
            if bucket_of(cfg.connection_id) != bucket:
                continue
            if not key.startswith(cfg.prefix):
                continue
            if cfg.extensions and not any(key.lower().endswith(e) for e in cfg.extensions):
                continue
            out.append(
                TriggerMatch(
                    workflow_id=wf_id,
                    workflow_version=version,
                    graph_json=graph_json,
                    node_id=node.id,
                    connection_id=cfg.connection_id,
                )
            )
    return out


class S3TriggerConsumer:
    """SQS を購読して s3_event ワークフローを発火する。

    sqs は boto3 クライアント互換（receive_message/delete_message）を注入。
    fetch は (bucket, key) -> bytes、ingest は UploadInput -> IngestResult。
    """

    def __init__(
        self,
        *,
        sqs: Any,
        queue_url: str,
        store: TriggerStore,
        fetch: Callable[[str, str], bytes],
        ingest: Callable[[Any], Any],
        enqueue: Callable[[str, dict[str, Any]], None],
        poll_interval_sec: float = POLL_INTERVAL_SEC,
    ) -> None:
        self._sqs = sqs
        self._queue_url = queue_url
        self._store = store
        self._fetch = fetch
        self._ingest = ingest
        self._enqueue = enqueue
        self._interval = poll_interval_sec
        self._last_poll = 0.0
        self._polls = 0

    def run_once(self) -> int:
        now = time.monotonic()
        if now - self._last_poll < self._interval:
            return 0
        self._last_poll = now

        # watch_lag_seconds（§16.8）: SQS の最古メッセージ滞留時間。毎回だと API が
        # 無駄なので 12 回に 1 回（約 1 分毎）だけ測る
        self._polls += 1
        if self._polls % 12 == 1:
            try:
                attrs = self._sqs.get_queue_attributes(
                    QueueUrl=self._queue_url,
                    AttributeNames=["ApproximateAgeOfOldestMessage"],
                )
                age = float(attrs.get("Attributes", {}).get("ApproximateAgeOfOldestMessage", 0))
                watch_lag_seconds.labels(connection="workflow-trigger").set(age)
            except Exception:  # noqa: BLE001 - 計測失敗で本処理を止めない
                logger.debug("[trigger] watch_lag の取得に失敗", exc_info=True)

        resp = self._sqs.receive_message(
            QueueUrl=self._queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=0
        )
        processed = 0
        for msg in resp.get("Messages", []) or []:
            try:
                done = self._process(json.loads(msg["Body"]))
            except Exception:  # noqa: BLE001 - 削除しない → visibility timeout 後に再配信（→DLQ）
                logger.exception("[trigger] イベント処理に失敗（再配信に委ねる）")
                continue
            if not done:
                continue  # 処理できる状態でない（削除しない）。再配信 → 5 回で DLQ
            self._sqs.delete_message(
                QueueUrl=self._queue_url, ReceiptHandle=msg["ReceiptHandle"]
            )
            processed += 1
        return processed

    # --- 1 イベントの処理 ---

    def _process(self, body: dict[str, Any]) -> bool:
        """1 イベントを処理する。True なら SQS から削除してよい（skip 含む）。

        False は「今は処理できる状態でない」（削除しない）。再配信 → 5 回で DLQ に
        退避され、状態が直った後に redrive（aws_env.sh redrive）で戻す。
        """
        detail = body.get("detail") or {}
        bucket = ((detail.get("bucket") or {}).get("name")) or ""
        obj = detail.get("object") or {}
        key = obj.get("key") or ""
        etag = obj.get("etag") or ""
        if not bucket or not key:
            logger.info("[trigger] S3 イベントでないため skip: %s", body.get("detail-type"))
            return True
        if "/" not in key:
            # 規約: 最上位フォルダ = テナント ID。規約外は誰の帳票か決められない
            logger.warning("[trigger] キーがテナント規約外のため skip: %s", key)
            return True
        tenant_id, rel_key = key.split("/", 1)
        if not tenant_id or not rel_key:
            logger.warning("[trigger] キーがテナント規約外のため skip: %s", key)
            return True

        workflows = self._store.list_active_workflows(tenant_id)
        if not workflows:
            # active が 1 つも無いのは「定義がまだ無い」状態。down（DB destroy）→ up 直後は
            # ここに該当し、定義を復元する前に滞留イベントを消化すると全部失われる。
            # 削除せず残す（→DLQ）。定義復元後に redrive すれば処理される。
            logger.warning(
                "[trigger] active なワークフローが無いため保留（削除しない）: tenant=%s key=%s",
                tenant_id, key,
            )
            return False
        matches = match_s3_event(
            workflows,
            lambda cid: self._store.get_s3_connection_bucket(tenant_id, cid),
            bucket,
            rel_key,
        )
        if not matches:
            logger.info("[trigger] 一致するワークフローなし: tenant=%s key=%s", tenant_id, key)
            return True

        # 早期 skip（DD-13）。ingest（S3 取得+前処理書込み）の前に安価に判定する
        if all(
            self._store.already_claimed(tenant_id, m.connection_id, key, etag)
            for m in matches
        ):
            logger.info("[trigger] 取込済みのため skip: %s (etag=%s)", key, etag)
            return True

        content = self._fetch(bucket, key)
        document_id = f"doc_{uuid.uuid4().hex[:24]}"

        from newfan_ingest import UploadInput  # 遅延 import（core は pydantic のみ）

        result = self._ingest(
            UploadInput(
                tenant_id=tenant_id,
                document_id=document_id,
                filename=posixpath.basename(key) or "upload.bin",
                content=content,
                declared_mime=None,
                doc_type=None,
                external_ref=f"s3://{bucket}/{key}",
            )
        )

        run_ids = self._store.register_ingested(
            tenant_id,
            source_key=key,
            content_hash=etag,
            document={
                "id": document_id,
                "storage_uri": result.storage_uri,
                "original_name": posixpath.basename(key),
                "mime_type": result.mime_type,
                "page_count": result.page_count,
                "external_ref": f"s3://{bucket}/{key}",
            },
            pages=[
                {
                    "page_no": p.page_no,
                    "width": p.width,
                    "height": p.height,
                    "image_uri": p.image_uri,
                    "preproc": p.preproc,
                }
                for p in result.pages
            ],
            matches=matches,
        )
        if not run_ids:
            # register 内の claim で競合（他プロセスが先に取込済み）
            logger.info("[trigger] claim 競合のため skip: %s", key)
            return True
        for run_id in run_ids:
            self._enqueue(
                "q.workflow",
                {"type": "start", "tenant_id": tenant_id, "workflow_run_id": run_id},
            )
        logger.info(
            "[trigger] 取込完了: tenant=%s key=%s document=%s runs=%s",
            tenant_id, key, document_id, run_ids,
        )
        return True


def match_gdrive_event(
    workflows: list[tuple[str, int, dict[str, Any]]],
    connection_id: str,
    filename: str,
) -> list[TriggerMatch]:
    """active ワークフローの gdrive_event ノードとファイルを突き合わせる（純関数）。"""
    out: list[TriggerMatch] = []
    for wf_id, version, graph_json in workflows:
        try:
            wf = WorkflowGraph.model_validate(graph_json)
        except Exception:  # noqa: BLE001 - 壊れた定義でトリガー全体を止めない
            logger.warning("[gdrive] graph_json が読めないため除外: workflow=%s", wf_id)
            continue
        for node in wf.nodes:
            if not isinstance(node, GDriveEventNode):
                continue
            cfg = node.config
            if cfg.connection_id != connection_id:
                continue
            if cfg.extensions and not any(
                filename.lower().endswith(e) for e in cfg.extensions
            ):
                continue
            out.append(
                TriggerMatch(
                    workflow_id=wf_id,
                    workflow_version=version,
                    graph_json=graph_json,
                    node_id=node.id,
                    connection_id=cfg.connection_id,
                )
            )
    return out


class GDrivePoller:
    """Google Drive フォルダの差分検知（⑤⑥ / 常駐ゼロ）。

    orchestrator-worker の主ループから run_once される（ScheduleTicker と同型）。
    常駐プロセスは増やさず、環境 up の間だけ interval おきにポーリングする。
    down 中の新着はファイルが Drive に残っているため次の up で遡って取り込まれる。

    冪等（DD-13）: (tenant, connection, "gdrive:"+file_id, content_hash) を
    source_cursors の UNIQUE で claim する（S3 トリガーと同一の仕組み・同一トランザクション）。
    同じファイルの再検知は skip、内容が変われば（hash 変化）再処理される。

    resolve_secret: secret_ref → リフレッシュトークン。FakeGDriveProvider は
    secret を使わないため未解決（None）でも動く。
    """

    def __init__(
        self,
        *,
        store: TriggerStore,
        provider: GDriveProvider,
        ingest: Callable[[Any], Any],
        enqueue: Callable[[str, dict[str, Any]], None],
        resolve_secret: Optional[Callable[[str], str]] = None,
        poll_interval_sec: float = 300.0,
    ) -> None:
        self._store = store
        self._provider = provider
        self._ingest = ingest
        self._enqueue = enqueue
        self._resolve_secret = resolve_secret
        self._interval = poll_interval_sec
        # 「未実行」は None。0.0 を番兵にすると、起動直後のホスト（monotonic が
        # interval 未満）で初回ポーリングまで間引かれる（CI の Linux VM で実際に発生）
        self._last_poll: Optional[float] = None

    def run_once(self) -> int:
        now = time.monotonic()
        if self._last_poll is not None and now - self._last_poll < self._interval:
            return 0
        self._last_poll = now
        return self.poll_all()

    def poll_all(self) -> int:
        processed = 0
        for tenant_id in self._store.list_tenant_ids():
            try:
                processed += self._poll_tenant(tenant_id)
            except Exception:  # noqa: BLE001 - 1テナントの失敗で他を止めない
                logger.exception("[gdrive] テナントのポーリングに失敗: %s", tenant_id)
        return processed

    def sync_now(self, tenant_id: str, connection_id: str) -> int:
        """「今すぐ同期」（gateway の POST /connections/{id}/sync から）。"""
        workflows = self._store.list_active_workflows(tenant_id)
        return self._poll_connection(tenant_id, connection_id, workflows)

    # --- 内部 ---

    def _poll_tenant(self, tenant_id: str) -> int:
        workflows = self._store.list_active_workflows(tenant_id)
        if not workflows:
            return 0
        # gdrive_event ノードが参照する接続を集める（同じ接続は1回だけ見る）
        connection_ids: list[str] = []
        for _wf_id, _version, graph_json in workflows:
            try:
                wf = WorkflowGraph.model_validate(graph_json)
            except Exception:  # noqa: BLE001
                continue
            for node in wf.nodes:
                if isinstance(node, GDriveEventNode):
                    cid = node.config.connection_id
                    if cid not in connection_ids:
                        connection_ids.append(cid)
        processed = 0
        for cid in connection_ids:
            try:
                processed += self._poll_connection(tenant_id, cid, workflows)
            except Exception:  # noqa: BLE001 - 1接続の失敗で他を止めない
                logger.exception(
                    "[gdrive] 接続のポーリングに失敗: tenant=%s connection=%s", tenant_id, cid
                )
        return processed

    def _poll_connection(
        self,
        tenant_id: str,
        connection_id: str,
        workflows: list[tuple[str, int, dict[str, Any]]],
    ) -> int:
        conn = self._store.get_gdrive_connection(tenant_id, connection_id)
        if conn is None:
            logger.warning(
                "[gdrive] gdrive 接続が見つからないため skip: tenant=%s connection=%s",
                tenant_id, connection_id,
            )
            return 0
        secret: Optional[str] = None
        if conn.secret_ref and self._resolve_secret is not None:
            secret = self._resolve_secret(conn.secret_ref)
        files = self._provider.list_files(folder_id=conn.folder_id, secret=secret)
        # フォルダの一覧取得に成功＝疎通OK。gdrive の疎通テストは gateway からは
        # できない（プロバイダは worker 側にしか無い）ため、同期の成功をもって
        # untested → tested に昇格させる（lint L010 / connection_ok が要求する状態）。
        self._store.mark_connection_tested(tenant_id, connection_id)
        processed = 0
        for f in files:
            matches = match_gdrive_event(workflows, connection_id, f.name)
            if not matches:
                continue
            # 早期 skip（DD-13）。download の前に安価に判定する
            source_key = f"gdrive:{f.id}"
            if all(
                self._store.already_claimed(tenant_id, m.connection_id, source_key, f.content_hash)
                for m in matches
            ):
                continue

            content = self._provider.download(file_id=f.id, secret=secret)
            document_id = f"doc_{uuid.uuid4().hex[:24]}"

            from newfan_ingest import UploadInput  # 遅延 import（core は pydantic のみ）

            result = self._ingest(
                UploadInput(
                    tenant_id=tenant_id,
                    document_id=document_id,
                    filename=f.name or "upload.bin",
                    content=content,
                    declared_mime=None,
                    doc_type=None,
                    external_ref=f"gdrive://{conn.folder_id}/{f.name}",
                )
            )
            run_ids = self._store.register_ingested(
                tenant_id,
                source_key=source_key,
                content_hash=f.content_hash,
                document={
                    "id": document_id,
                    "storage_uri": result.storage_uri,
                    "original_name": f.name,
                    "mime_type": result.mime_type,
                    "page_count": result.page_count,
                    "external_ref": f"gdrive://{conn.folder_id}/{f.name}",
                },
                pages=[
                    {
                        "page_no": p.page_no,
                        "width": p.width,
                        "height": p.height,
                        "image_uri": p.image_uri,
                        "preproc": p.preproc,
                    }
                    for p in result.pages
                ],
                matches=matches,
            )
            if not run_ids:
                continue  # claim 競合（別 consumer が先に取込済み）
            for run_id in run_ids:
                self._enqueue(
                    "q.workflow",
                    {"type": "start", "tenant_id": tenant_id, "workflow_run_id": run_id},
                )
            processed += 1
            logger.info(
                "[gdrive] 取込完了: tenant=%s file=%s document=%s runs=%s",
                tenant_id, f.name, document_id, run_ids,
            )
        return processed


class InMemoryTriggerStore:
    """テスト用。"""

    def __init__(self) -> None:
        self.workflows: dict[str, list[tuple[str, int, dict[str, Any]]]] = {}
        self.s3_connections: dict[tuple[str, str], str] = {}
        self.gdrive_connections: dict[tuple[str, str], GDriveConnection] = {}
        self.claims: set[tuple[str, str, str, str]] = set()
        self.documents: list[dict[str, Any]] = []
        self.runs: list[dict[str, Any]] = []
        self._seq = 0

    def seed_workflow(
        self, tenant_id: str, workflow_id: str, version: int, graph_json: dict[str, Any]
    ) -> None:
        self.workflows.setdefault(tenant_id, []).append((workflow_id, version, graph_json))

    def seed_s3_connection(self, tenant_id: str, connection_id: str, bucket: str) -> None:
        self.s3_connections[(tenant_id, connection_id)] = bucket

    def seed_gdrive_connection(
        self, tenant_id: str, connection_id: str, folder_id: str, secret_ref: Optional[str] = None
    ) -> None:
        self.gdrive_connections[(tenant_id, connection_id)] = GDriveConnection(
            folder_id=folder_id, secret_ref=secret_ref
        )

    def list_active_workflows(self, tenant_id: str) -> list[tuple[str, int, dict[str, Any]]]:
        return list(self.workflows.get(tenant_id, []))

    def get_s3_connection_bucket(self, tenant_id: str, connection_id: str) -> Optional[str]:
        return self.s3_connections.get((tenant_id, connection_id))

    def get_gdrive_connection(
        self, tenant_id: str, connection_id: str
    ) -> Optional[GDriveConnection]:
        return self.gdrive_connections.get((tenant_id, connection_id))

    def mark_connection_tested(self, tenant_id: str, connection_id: str) -> None:
        self.tested_connections = getattr(self, "tested_connections", set())
        self.tested_connections.add((tenant_id, connection_id))

    def already_claimed(self, tenant_id, connection_id, source_key, content_hash) -> bool:
        return (tenant_id, connection_id, source_key, content_hash) in self.claims

    def list_tenant_ids(self) -> list[str]:
        return sorted(self.workflows.keys())

    def register_scheduled_run(
        self, tenant_id, *, workflow_id, workflow_version, graph_json, node_id, fire_minute
    ):
        key = (tenant_id, node_id, f"schedule:{workflow_id}", fire_minute)
        if key in self.claims:
            return None
        self.claims.add(key)
        self._seq += 1
        run_id = f"wfrun_sched_{self._seq}"
        self.runs.append(
            {
                "id": run_id,
                "tenant_id": tenant_id,
                "workflow_id": workflow_id,
                "document_id": None,
                "source_key": f"schedule:{workflow_id}",
                "fired_at": fire_minute,
            }
        )
        return run_id

    def register_ingested(
        self, tenant_id, *, source_key, content_hash, document, pages, matches
    ) -> list[str]:
        claimed: set[str] = set()
        for m in matches:
            key = (tenant_id, m.connection_id, source_key, content_hash)
            if key in self.claims:
                continue
            self.claims.add(key)
            claimed.add(m.connection_id)
        if not claimed:
            return []
        self.documents.append({**document, "tenant_id": tenant_id, "pages": pages})
        run_ids: list[str] = []
        for m in matches:
            if m.connection_id not in claimed:
                continue
            self._seq += 1
            run_id = f"wfrun_trig_{self._seq}"
            self.runs.append(
                {
                    "id": run_id,
                    "tenant_id": tenant_id,
                    "workflow_id": m.workflow_id,
                    "document_id": document["id"],
                    "source_key": source_key,
                }
            )
            run_ids.append(run_id)
        return run_ids


class ScheduleTicker:
    """source.schedule の分ティック評価（§16 P8）。

    設計 v0.2 §7.3 は「EventBridge Scheduler → SQS」としていたが、cron の
    Scheduler 同期（activate/pause/版更新のたびに AWS リソースを増減）は
    down（destroy）運用と噛み合わないため、**consumer 内の分ティック**に変更した
    （設計書に反映済み）。up の間だけ評価し、down 中の発火はしない
    （§1.3: 即時処理の保証はスコープ外。s3_event と違い「置かれたファイル」が
    無いので、過ぎた時刻の遡り発火はしない）。

    dedup は source_cursors の UNIQUE（workflow+node+分時刻）。consumer が
    複数居ても 1 分につき 1 run になる。直近 5 分まで遡って評価する
    （ループが数十秒詰まった程度で日次ジョブを落とさないため）。
    """

    LOOKBACK_MINUTES = 5

    def __init__(
        self,
        *,
        store: TriggerStore,
        enqueue: Callable[[str, dict[str, Any]], None],
    ) -> None:
        self._store = store
        self._enqueue = enqueue
        self._last_minute: Optional[datetime] = None

    def run_once(self, now: Optional[datetime] = None) -> int:
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        minute = now.replace(second=0, microsecond=0)
        if self._last_minute is not None and minute <= self._last_minute:
            return 0
        start = self._last_minute or (minute - timedelta(minutes=1))
        start = max(start, minute - timedelta(minutes=self.LOOKBACK_MINUTES))
        fired = 0
        m = start + timedelta(minutes=1)
        while m <= minute:
            fired += self._fire_minute(m)
            m += timedelta(minutes=1)
        self._last_minute = minute
        return fired

    def _fire_minute(self, minute: datetime) -> int:
        fired = 0
        for tenant_id in self._store.list_tenant_ids():
            for wf_id, version, graph_json in self._store.list_active_workflows(tenant_id):
                try:
                    wf = WorkflowGraph.model_validate(graph_json)
                except Exception:  # noqa: BLE001 - 壊れた定義で他を止めない
                    continue
                for node in wf.nodes:
                    if not isinstance(node, ScheduleNode):
                        continue
                    try:
                        if not cron_matches(node.config.cron, minute):
                            continue
                    except CronError:
                        logger.warning(
                            "[schedule] cron 式が不正のため skip: workflow=%s node=%s",
                            wf_id, node.id,
                        )
                        continue
                    run_id = self._store.register_scheduled_run(
                        tenant_id,
                        workflow_id=wf_id,
                        workflow_version=version,
                        graph_json=graph_json,
                        node_id=node.id,
                        fire_minute=minute.astimezone(JST).strftime("%Y-%m-%dT%H:%M%z"),
                    )
                    if run_id is None:
                        continue  # 別 consumer が発火済み（dedup）
                    self._enqueue(
                        "q.workflow",
                        {"type": "start", "tenant_id": tenant_id, "workflow_run_id": run_id},
                    )
                    logger.info(
                        "[schedule] 発火: tenant=%s workflow=%s node=%s run=%s",
                        tenant_id, wf_id, node.id, run_id,
                    )
                    fired += 1
        return fired
