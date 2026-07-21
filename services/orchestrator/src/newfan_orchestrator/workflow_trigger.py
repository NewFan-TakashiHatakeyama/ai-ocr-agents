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
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol

from newfan_workflow import WorkflowGraph
from newfan_workflow.models import S3EventNode

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


class TriggerStore(Protocol):
    def list_active_workflows(self, tenant_id: str) -> list[tuple[str, int, dict[str, Any]]]: ...
    def get_s3_connection_bucket(self, tenant_id: str, connection_id: str) -> Optional[str]: ...
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

    def run_once(self) -> int:
        now = time.monotonic()
        if now - self._last_poll < self._interval:
            return 0
        self._last_poll = now

        resp = self._sqs.receive_message(
            QueueUrl=self._queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=0
        )
        processed = 0
        for msg in resp.get("Messages", []) or []:
            try:
                self._process(json.loads(msg["Body"]))
            except Exception:  # noqa: BLE001 - 削除しない → visibility timeout 後に再配信（→DLQ）
                logger.exception("[trigger] イベント処理に失敗（再配信に委ねる）")
                continue
            self._sqs.delete_message(
                QueueUrl=self._queue_url, ReceiptHandle=msg["ReceiptHandle"]
            )
            processed += 1
        return processed

    # --- 1 イベントの処理 ---

    def _process(self, body: dict[str, Any]) -> None:
        detail = body.get("detail") or {}
        bucket = ((detail.get("bucket") or {}).get("name")) or ""
        obj = detail.get("object") or {}
        key = obj.get("key") or ""
        etag = obj.get("etag") or ""
        if not bucket or not key:
            logger.info("[trigger] S3 イベントでないため skip: %s", body.get("detail-type"))
            return
        if "/" not in key:
            # 規約: 最上位フォルダ = テナント ID。規約外は誰の帳票か決められない
            logger.warning("[trigger] キーがテナント規約外のため skip: %s", key)
            return
        tenant_id, rel_key = key.split("/", 1)
        if not tenant_id or not rel_key:
            logger.warning("[trigger] キーがテナント規約外のため skip: %s", key)
            return

        workflows = self._store.list_active_workflows(tenant_id)
        matches = match_s3_event(
            workflows,
            lambda cid: self._store.get_s3_connection_bucket(tenant_id, cid),
            bucket,
            rel_key,
        )
        if not matches:
            logger.info("[trigger] 一致するワークフローなし: tenant=%s key=%s", tenant_id, key)
            return

        # 早期 skip（DD-13）。ingest（S3 取得+前処理書込み）の前に安価に判定する
        if all(
            self._store.already_claimed(tenant_id, m.connection_id, key, etag)
            for m in matches
        ):
            logger.info("[trigger] 取込済みのため skip: %s (etag=%s)", key, etag)
            return

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
            return
        for run_id in run_ids:
            self._enqueue(
                "q.workflow",
                {"type": "start", "tenant_id": tenant_id, "workflow_run_id": run_id},
            )
        logger.info(
            "[trigger] 取込完了: tenant=%s key=%s document=%s runs=%s",
            tenant_id, key, document_id, run_ids,
        )


class InMemoryTriggerStore:
    """テスト用。"""

    def __init__(self) -> None:
        self.workflows: dict[str, list[tuple[str, int, dict[str, Any]]]] = {}
        self.s3_connections: dict[tuple[str, str], str] = {}
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

    def list_active_workflows(self, tenant_id: str) -> list[tuple[str, int, dict[str, Any]]]:
        return list(self.workflows.get(tenant_id, []))

    def get_s3_connection_bucket(self, tenant_id: str, connection_id: str) -> Optional[str]:
        return self.s3_connections.get((tenant_id, connection_id))

    def already_claimed(self, tenant_id, connection_id, source_key, content_hash) -> bool:
        return (tenant_id, connection_id, source_key, content_hash) in self.claims

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
