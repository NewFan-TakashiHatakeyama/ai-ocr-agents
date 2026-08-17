# mypy: ignore-errors
"""orchestrator-svc 常駐エントリ（`python -m newfan_orchestrator.worker_main`, §2.1 / §9）。

環境変数から本番アダプタを配線し、q.extract を消費し続ける ECS タスク本体。
gateway の `newfan_gateway.main` に対応する。SIGTERM で graceful shutdown。

必須 env: DATABASE_URL, REDIS_URL, STRUCTURE_URL
任意 env: VL_URL（既定無効, ADR-0003 Option A）, LLM_MODEL, CONSUMER_NAME, POLL_BLOCK_MS

チェックポイントは PostgresSaver（プロセス跨ぎ resume, §4.4）。structure/LLM は実クライアント。
memory は PgMemoryRepository（正本=PostgreSQL, §5.8.3）＋e5 埋め込み。FAISS index は
list_memories からプロセス毎に再構築（MemoryService._rehydrate）。画像は s3:///file:// 両対応。
"""

from __future__ import annotations

import logging
import os
import signal
import socket
from typing import Any

from newfan_llm_adapter import LLMAdapter, PromptBundle, default_bundle_dir
from newfan_memory import MemoryService
from newfan_paddle_client import PaddleServingClient

from newfan_orchestrator.graph import build_graph
from newfan_orchestrator.image_loaders import make_dispatching_image_loader
from newfan_orchestrator.pg_persistence import PgContextStore
from newfan_orchestrator.redis_io import RedisQueue, RedisStreamConsumer
from newfan_orchestrator.serde import newfan_serde
from newfan_orchestrator.worker import ExtractionWorker
from newfan_orchestrator.workflow_graph import RunnerDeps
from newfan_orchestrator.workflow_runner import WorkflowRunner
from newfan_orchestrator.workflow_store import PgTriggerStore, PgWorkflowRunStore
from newfan_orchestrator.workflow_trigger import S3TriggerConsumer

_STOP = False


def _handle_sigterm(*_: Any) -> None:
    global _STOP
    _STOP = True


def _pg_dsn() -> str:
    # PostgresSaver / psycopg は plain な postgresql:// を期待（SQLAlchemy の +psycopg を除去）。
    return os.environ["DATABASE_URL"].replace("+psycopg", "")


def _wf_dsn() -> str:
    """ワークフロー層 checkpointer の接続（lg_wf スキーマ, §16 設計 v0.2 §1.2）。

    PostgresSaver はテーブル名が固定・スキーマ非修飾のため、search_path を接続文字列に
    焼いて抽出グラフ（public）と分離する。分離が効くことは実測済み。SET で後から
    切り替える方式は接続プール経由の混線があり得るため使わない。
    """
    dsn = _pg_dsn()
    sep = "&" if "?" in dsn else "?"
    return f"{dsn}{sep}options=-csearch_path%3Dlg_wf"


def _memory(adapter: LLMAdapter, bundle: PromptBundle) -> MemoryService:
    # 正本は PostgreSQL（§5.8.3）。DATABASE_URL 未設定時のみ InMemory へ degrade。
    dsn = os.environ.get("DATABASE_URL")
    if dsn:
        from newfan_memory.pg_memory import PgMemoryRepository

        repo: Any = PgMemoryRepository(dsn)
    else:
        from newfan_memory import InMemoryMemoryRepository

        repo = InMemoryMemoryRepository()

    try:
        from newfan_memory.e5_embedder import E5Embedder

        embedder: Any = E5Embedder()
    except Exception:  # noqa: BLE001 - 埋め込み extra 未導入時は degrade
        from newfan_memory import HashingEmbedder

        embedder = HashingEmbedder()
    # adapter/bundle を渡さないと learn が「_adapter is None」で即 return し、
    # 修正が何件たまってもルール抽出（§5.8.4）が一度も走らない。実 AWS で
    # tenant_memories は増えるのに tenant_rules が 0 件のままなのはこれが原因だった。
    return MemoryService(embedder, repo, adapter=adapter, bundle=bundle)


def _make_provider() -> Any:
    # LLM_PROVIDER=gemini か GEMINI_API_KEY 設定時は Gemini、既定は Anthropic。
    provider = os.environ.get("LLM_PROVIDER", "").lower()
    if provider == "gemini" or (not provider and os.environ.get("GEMINI_API_KEY")):
        from newfan_llm_adapter.gemini_provider import GeminiProvider

        return GeminiProvider(model=os.environ.get("LLM_MODEL", "gemini-2.5-flash"))
    from newfan_llm_adapter.anthropic_provider import AnthropicProvider

    return AnthropicProvider(model=os.environ.get("LLM_MODEL", "claude-opus-4-8"))


def main() -> None:
    # ログ設定が無いと worker.run_once の logger.exception が出力されず、ジョブ失敗が
    # 「pending が増えるだけの無言」になって本番で原因究明できない（実コンテナで検出）。
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)

    store = PgContextStore(os.environ["DATABASE_URL"])
    export_queue = RedisQueue(os.environ["REDIS_URL"])
    consumer = RedisStreamConsumer(
        os.environ["REDIS_URL"],
        "q.extract",
        "orchestrator",
        os.environ.get("CONSUMER_NAME", socket.gethostname()),
    )
    # PP-StructureV3 は 1 枚 16.4 秒（4vCPU 実測・ウォーム時）かかり、コンテナ起動直後の
    # 初回はモデルのウォームアップでさらに延びる。クライアント既定の 30 秒では初回が
    # ReadTimeout で落ち、構造抽出が丸ごと失われて LLM が幻覚を返す（実 AWS で検出）。
    timeout = float(os.environ.get("INFERENCE_TIMEOUT_SEC", "180"))
    structure = PaddleServingClient(os.environ["STRUCTURE_URL"], timeout=timeout)
    vl = PaddleServingClient(os.environ["VL_URL"], timeout=timeout) if os.environ.get("VL_URL") else None
    # DD-02 char_backfill 用の /ocr。未設定なら補完なし（主経路のみ）。
    ocr = (
        PaddleServingClient(os.environ["OCR_URL"], timeout=timeout)
        if os.environ.get("OCR_URL")
        else None
    )
    adapter = LLMAdapter(_make_provider())
    bundle = PromptBundle.load(default_bundle_dir())

    from langgraph.checkpoint.postgres import PostgresSaver

    with (
        PostgresSaver.from_conn_string(_pg_dsn()) as checkpointer,
        PostgresSaver.from_conn_string(_wf_dsn()) as wf_checkpointer,
    ):
        # setup() はここでは呼ばない。チェックポイント表の DDL はスキーマ変更であり
        # migrate の仕事（scripts/setup_checkpointer.py）。§7.3 でアプリは所有者でない
        # ロールに移したため、ワーカーには schema public への CREATE 権限が無く、
        # ここで setup() すると permission denied で全ジョブが落ちる（実 AWS で踏んだ）。
        checkpointer.serde = newfan_serde()  # schema 型を許可（未登録型ブロック回避, §4.4）
        graph = build_graph(
            checkpointer=checkpointer,
            adapter=adapter,
            bundle=bundle,
            memory=_memory(adapter, bundle),
            structure_client=structure,
            vl_client=vl,
            ocr_client=ocr,
            image_loader=make_dispatching_image_loader(),  # s3:// / file:// を振り分け
            context_store=store,
            export_enqueue=export_queue.enqueue,
        )
        worker = ExtractionWorker(
            graph, store, consumer,
            enqueue=export_queue.enqueue,
            usage_snapshot=adapter.usage_snapshot,
        )

        # workflow-runner（§16 設計 v0.2 §6）。q.workflow を同じプロセスで消費する。
        # 常駐プロセスは増やさない（コスト。設計 §2.1）。
        from newfan_export.webhook import WebhookSender

        from newfan_orchestrator.aws_secrets import SecretsManagerResolver
        from newfan_orchestrator.workflow_sinks import NotifySender, PgDbWriter, S3FileWriter

        resolve_secret = SecretsManagerResolver()
        wf_store = PgWorkflowRunStore(
            os.environ["DATABASE_URL"],
            enqueue=export_queue.enqueue,
            resolve_secret=resolve_secret,
        )
        wf_consumer = RedisStreamConsumer(
            os.environ["REDIS_URL"],
            "q.workflow",
            "workflow-runner",
            os.environ.get("CONSUMER_NAME", socket.gethostname()),
        )
        runner = WorkflowRunner(
            wf_store,
            wf_consumer,
            RunnerDeps(
                store=wf_store,
                send_webhook=WebhookSender().send,
                write_db=PgDbWriter().write,
                resolve_secret=resolve_secret,
                write_file=S3FileWriter(
                    kms_key_id=os.environ.get("S3_KMS_KEY_ID") or None
                ).write,
                send_notify=NotifySender().send,
            ),
            checkpointer=wf_checkpointer,
        )
        # source.schedule の分ティック（§16 P8）。トリガー store と共用
        from newfan_orchestrator.workflow_trigger import ScheduleTicker

        trigger_store = PgTriggerStore(os.environ["DATABASE_URL"])
        ticker = ScheduleTicker(store=trigger_store, enqueue=export_queue.enqueue)

        # S3 イベント駆動トリガー（§16 設計 v0.2 §7.2）。TRIGGER_SQS_URL 未設定なら無効
        #（compose/ローカルは手動実行のみ）。
        trigger = None
        if os.environ.get("TRIGGER_SQS_URL"):
            import boto3

            from newfan_ingest import IngestService
            from newfan_ingest.rasterize import AutoRasterizer
            from newfan_ingest.storage import S3ObjectStore

            s3 = boto3.client("s3")
            ingest = IngestService(
                S3ObjectStore(
                    os.environ["S3_BUCKET"],
                    kms_key_id=os.environ.get("S3_KMS_KEY_ID") or None,
                ),
                AutoRasterizer(),
            )
            trigger = S3TriggerConsumer(
                sqs=boto3.client("sqs"),
                queue_url=os.environ["TRIGGER_SQS_URL"],
                store=trigger_store,
                fetch=lambda b, k: s3.get_object(Bucket=b, Key=k)["Body"].read(),
                ingest=ingest.ingest,
                enqueue=export_queue.enqueue,
            )
            print("[worker] trigger consumer 有効: " + os.environ["TRIGGER_SQS_URL"])

        # SaaS フォルダ監視トリガー（⑤⑥ / 常駐ゼロ方針）。gdrive/m365/box で同型。
        # 常駐プロセスは増やさず、この主ループが up の間だけ interval おきに差分検知する。
        # 種別ごとの選択: <TYPE>_FAKE_ROOT（開発: ローカル dir を SaaS フォルダに見立てる）
        # → 実 API の env が揃った時（モックファースト方針のため実アカウント E2E は未了）
        # → どちらも無ければその種別は無効。
        from newfan_workflow.models import BoxEventNode, GDriveEventNode, M365EventNode

        def _make_saas_provider(kind: str) -> Any:
            fake_root = os.environ.get(f"{kind.upper()}_FAKE_ROOT")
            if fake_root:
                from newfan_orchestrator.gdrive import FakeGDriveProvider

                print(f"[worker] {kind}: Fake(ローカルdir) 有効 root={fake_root}")
                return FakeGDriveProvider(fake_root)
            if kind == "gdrive" and os.environ.get("GOOGLE_OAUTH_CLIENT_ID") and os.environ.get(
                "GOOGLE_OAUTH_CLIENT_SECRET"
            ):
                from newfan_orchestrator.gdrive import GoogleDriveProvider

                print("[worker] gdrive: GoogleDriveProvider 有効")
                return GoogleDriveProvider(
                    os.environ["GOOGLE_OAUTH_CLIENT_ID"], os.environ["GOOGLE_OAUTH_CLIENT_SECRET"]
                )
            if kind == "m365" and all(
                os.environ.get(k) for k in ("M365_TENANT_ID", "M365_CLIENT_ID", "M365_CLIENT_SECRET")
            ):
                from newfan_orchestrator.m365box import GraphFolderProvider

                print("[worker] m365: GraphFolderProvider 有効")
                return GraphFolderProvider(
                    os.environ["M365_TENANT_ID"],
                    os.environ["M365_CLIENT_ID"],
                    os.environ["M365_CLIENT_SECRET"],
                )
            if kind == "box" and all(
                os.environ.get(k) for k in ("BOX_CLIENT_ID", "BOX_CLIENT_SECRET", "BOX_ENTERPRISE_ID")
            ):
                from newfan_orchestrator.m365box import BoxFolderProvider

                print("[worker] box: BoxFolderProvider 有効")
                return BoxFolderProvider(
                    os.environ["BOX_CLIENT_ID"],
                    os.environ["BOX_CLIENT_SECRET"],
                    os.environ["BOX_ENTERPRISE_ID"],
                )
            return None

        saas_pollers: dict[str, Any] = {}
        saas_sync_consumer = None
        saas_ingest: Any = None
        saas_interval = float(
            os.environ.get("SAAS_POLL_INTERVAL_SEC")
            or os.environ.get("GDRIVE_POLL_INTERVAL_SEC", "300")
        )
        for kind, node_cls in (
            ("gdrive", GDriveEventNode),
            ("m365", M365EventNode),
            ("box", BoxEventNode),
        ):
            saas_provider = _make_saas_provider(kind)
            if saas_provider is None:
                continue
            if saas_ingest is None:
                from newfan_ingest import IngestService
                from newfan_ingest.rasterize import AutoRasterizer

                # 保存先は gateway と同じ規約: S3_BUCKET があれば S3、無ければローカル FS
                if os.environ.get("S3_BUCKET"):
                    from newfan_ingest.storage import S3ObjectStore

                    object_store: Any = S3ObjectStore(
                        os.environ["S3_BUCKET"],
                        kms_key_id=os.environ.get("S3_KMS_KEY_ID") or None,
                    )
                else:
                    from pathlib import Path

                    from newfan_ingest.storage import LocalObjectStore

                    object_store = LocalObjectStore(Path(os.environ.get("STORAGE_ROOT", "/data")))
                saas_ingest = IngestService(object_store, AutoRasterizer())
            from newfan_orchestrator.workflow_trigger import FolderEventPoller

            saas_pollers[kind] = FolderEventPoller(
                store=trigger_store,
                provider=saas_provider,
                ingest=saas_ingest.ingest,
                enqueue=export_queue.enqueue,
                resolve_secret=resolve_secret,
                poll_interval_sec=saas_interval,
                conn_type=kind,
                node_cls=node_cls,
            )
        if saas_pollers:
            # 「今すぐ同期」（gateway の POST /connections/{id}/sync → q.sync）
            saas_sync_consumer = RedisStreamConsumer(
                os.environ["REDIS_URL"],
                "q.sync",
                "gdrive-sync",  # 既存グループ名を維持（デプロイ跨ぎの PEL 引継ぎ）
                os.environ.get("CONSUMER_NAME", socket.gethostname()),
            )
        # 同期失敗の再試行上限。transient は autoclaim 再配信で回復させるが、
        # 恒久失敗（folder_id タイポ等）を無限再試行する毒メッセージにしない。
        # 失敗結果は connections.last_sync_* に記録済みで UI から見える
        saas_sync_fail_counts: dict[str, int] = {}
        SAAS_SYNC_MAX_ATTEMPTS = 3

        print("[worker] orchestrator-svc 起動: q.extract / q.workflow を消費します")
        while not _STOP:
            worker.run_once()  # consume は block_ms 待機するため busy-loop にならない
            runner.run_once(count=10)
            if trigger is not None:
                trigger.run_once()  # 5 秒間隔で SQS をポーリング（内部で間引く）
            ticker.run_once()  # source.schedule の分ティック（分が変わった時だけ動く）
            if saas_pollers:
                try:
                    for saas_poller in saas_pollers.values():
                        saas_poller.run_once()  # interval おきに差分検知（内部で間引く）
                    # 注意: Redis の BLOCK 0 は「無限ブロック」。空キューで主ループが
                    # 固まりソケットタイムアウトで落ちる（実 compose で検出）→ 1ms
                    for msg_id, payload in saas_sync_consumer.consume(count=5, block_ms=1):
                        kind = payload.get("type", "gdrive")  # 旧メッセージ互換
                        target = saas_pollers.get(kind)
                        if target is None:
                            # この worker に未構成の種別。再配信しても直らないため
                            # ack して捨てる（構成後に「今すぐ同期」し直してもらう）
                            logging.getLogger(__name__).warning(
                                "[saas] 未構成の種別への同期要求を破棄: %s", kind
                            )
                            saas_sync_consumer.ack(msg_id)
                            continue
                        try:
                            n = target.sync_now(
                                payload.get("tenant_id", ""), payload.get("connection_id", "")
                            )
                            print(
                                f"[worker] {kind} 今すぐ同期: {payload.get('connection_id')} → {n} 件"
                            )
                            # 成功時のみ ack（at-least-once）。untested 接続の tested 昇格
                            # 経路は q.sync だけなので、transient 失敗で ack すると接続が
                            # untested に固着し WF 有効化が詰まる（レビュー確定）。失敗時は
                            # PEL に残し autoclaim(60s) の再配信で再試行。重複実行は
                            # source_cursors claim で冪等
                            saas_sync_consumer.ack(msg_id)
                            saas_sync_fail_counts.pop(msg_id, None)
                        except Exception:  # noqa: BLE001 - 同期失敗でループを止めない
                            logging.getLogger(__name__).exception("[%s] 今すぐ同期に失敗", kind)
                            n_fail = saas_sync_fail_counts.get(msg_id, 0) + 1
                            saas_sync_fail_counts[msg_id] = n_fail
                            if n_fail >= SAAS_SYNC_MAX_ATTEMPTS:
                                # 恒久失敗（folder_id タイポ等）の毒メッセージ化を防ぐ。
                                # 失敗結果は connections.last_sync_* に記録済み（UI で可視）
                                logging.getLogger(__name__).warning(
                                    "[%s] 同期が %d 回失敗したため破棄: %s",
                                    kind, n_fail, payload.get("connection_id"),
                                )
                                saas_sync_consumer.ack(msg_id)
                                saas_sync_fail_counts.pop(msg_id, None)
                except Exception:  # noqa: BLE001 - SaaS 区画の失敗で抽出/実行を殺さない
                    logging.getLogger(__name__).exception("[saas] ポーリング区画で例外")
    print("[worker] SIGTERM 受信: 停止しました")


if __name__ == "__main__":
    main()
