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

_STOP = False


def _handle_sigterm(*_: Any) -> None:
    global _STOP
    _STOP = True


def _pg_dsn() -> str:
    # PostgresSaver / psycopg は plain な postgresql:// を期待（SQLAlchemy の +psycopg を除去）。
    return os.environ["DATABASE_URL"].replace("+psycopg", "")


def _memory() -> MemoryService:
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
    return MemoryService(embedder, repo)


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
    structure = PaddleServingClient(os.environ["STRUCTURE_URL"])
    vl = PaddleServingClient(os.environ["VL_URL"]) if os.environ.get("VL_URL") else None
    # DD-02 char_backfill 用の /ocr。未設定なら補完なし（主経路のみ）。
    ocr = PaddleServingClient(os.environ["OCR_URL"]) if os.environ.get("OCR_URL") else None
    adapter = LLMAdapter(_make_provider())
    bundle = PromptBundle.load(default_bundle_dir())

    from langgraph.checkpoint.postgres import PostgresSaver

    with PostgresSaver.from_conn_string(_pg_dsn()) as checkpointer:
        checkpointer.setup()
        checkpointer.serde = newfan_serde()  # schema 型を許可（未登録型ブロック回避, §4.4）
        graph = build_graph(
            checkpointer=checkpointer,
            adapter=adapter,
            bundle=bundle,
            memory=_memory(),
            structure_client=structure,
            vl_client=vl,
            ocr_client=ocr,
            image_loader=make_dispatching_image_loader(),  # s3:// / file:// を振り分け
            context_store=store,
            export_enqueue=export_queue.enqueue,
        )
        worker = ExtractionWorker(graph, store, consumer)
        print("[worker] orchestrator-svc 起動: q.extract を消費します")
        while not _STOP:
            worker.run_once()  # consume は block_ms 待機するため busy-loop にならない
    print("[worker] SIGTERM 受信: 停止しました")


if __name__ == "__main__":
    main()
