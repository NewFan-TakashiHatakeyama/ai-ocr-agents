"""本番 ASGI エントリポイント（`uvicorn newfan_gateway.main:app`）。

環境変数から本番アダプタを配線する:
- DATABASE_URL 有  → PgRepository（RLS 付き, §7.3）。無ければ InMemoryRepository（開発）。
- REDIS_URL 有      → RedisQueue（q.extract/q.export, §9）＋ QueueOrchestratorClient（resume ジョブ発行, §4.4）。
                      無ければ InMemoryQueue ＋ FakeOrchestratorClient（開発）。

ingestor/api_keys は create_app の既定（本番 IngestService / 空の APIキーストア）に委ねる。
"""

from __future__ import annotations

import os

from fastapi import FastAPI

from newfan_gateway.app import create_app
from newfan_gateway.auth import ApiKeyStore, EnvApiKeyStore
from newfan_gateway.chat import ChatAgent
from newfan_gateway.config import Settings
from newfan_gateway.ports import OrchestratorClient
from newfan_gateway.queue import Queue
from newfan_gateway.repository import Repository


def _llm_provider() -> object | None:
    """チャット supervisor 用の LLM プロバイダ（worker_main._make_provider と同じ選択規則）。"""
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        from newfan_llm_adapter.gemini_provider import GeminiProvider

        return GeminiProvider(model=os.environ.get("LLM_MODEL", "gemini-2.5-flash"))
    if os.environ.get("ANTHROPIC_API_KEY"):
        from newfan_llm_adapter.anthropic_provider import AnthropicProvider

        return AnthropicProvider(model=os.environ.get("LLM_MODEL", "claude-opus-4-8"))
    return None


def build_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    repo: Repository | None = None
    admin = None
    if settings.database_url:
        from newfan_gateway.db import PgAdminRepository, PgRepository

        repo = PgRepository(settings.database_url)
        admin = PgAdminRepository(settings.database_url)

    queue: Queue | None = None
    orchestrator: OrchestratorClient | None = None
    if settings.redis_url:
        from newfan_gateway.prod import QueueOrchestratorClient, RedisQueue

        redis_queue = RedisQueue(settings.redis_url)
        queue = redis_queue
        orchestrator = QueueOrchestratorClient(redis_queue)

    # API キーは Secrets Manager 由来の環境変数 API_KEYS(JSON) から（未設定は空 InMemory）。
    api_keys: ApiKeyStore | None = EnvApiKeyStore.from_env() if os.environ.get("API_KEYS") else None

    # チャットエージェント（§4.5）:
    #   DB/キュー + LLM キーが揃う → SupervisorChatAgent（LangGraph。ツールが実データを読む）
    #   LLM キーのみ            → 旧 tool-use エージェント（navigate/update_schema のみ）
    #   どちらも無し            → RuleBasedChatAgent（決定論。dev/テスト）
    # supervisor のツールは repo/admin/queue を要するため、揃わない環境では従来動作に落とす。
    chat_agent: ChatAgent | None = None
    provider = _llm_provider()
    if provider is not None and repo is not None and admin is not None and queue is not None:
        from newfan_gateway.chat_graph_agent import SupervisorChatAgent
        from newfan_gateway.chat_tools import ChatTools

        chat_agent = SupervisorChatAgent(
            provider=provider,
            tools=ChatTools(repo=repo, admin=admin, queue=queue),
        )
    elif os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        from newfan_gateway.chat import GeminiChatAgent

        chat_agent = GeminiChatAgent(model=os.environ.get("LLM_MODEL", "gemini-2.5-flash"))
    elif os.environ.get("ANTHROPIC_API_KEY"):
        from newfan_gateway.chat import LlmChatAgent

        chat_agent = LlmChatAgent(model=os.environ.get("LLM_MODEL", "claude-opus-4-8"))

    return create_app(
        settings=settings,
        repo=repo,
        queue=queue,
        orchestrator=orchestrator,
        api_keys=api_keys,
        admin=admin,
        chat_agent=chat_agent,
    )


app = build_app()
