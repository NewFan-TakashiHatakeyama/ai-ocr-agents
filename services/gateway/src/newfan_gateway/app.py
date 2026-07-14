"""FastAPI アプリファクトリ。

依存（repo/queue/orchestrator/ingestor/api_keys）は引数で注入可能にし、テストは
InMemory 実装を渡す。本番は db.PgRepository / RedisQueue / HttpOrchestratorClient を渡す。
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from newfan_gateway.auth import ApiKeyStore, InMemoryApiKeyStore
from newfan_gateway.config import Settings
from newfan_gateway.context import request_id_var
from newfan_gateway.errors import ApiError, api_error_handler, error_body
from newfan_gateway.ids import new_id
from newfan_gateway.ports import FakeOrchestratorClient, Ingestor, OrchestratorClient
from newfan_gateway.queue import InMemoryQueue, Queue
from newfan_gateway.repository import InMemoryRepository, Repository
from newfan_gateway.routers import router


def create_app(
    *,
    settings: Optional[Settings] = None,
    repo: Optional[Repository] = None,
    queue: Optional[Queue] = None,
    orchestrator: Optional[OrchestratorClient] = None,
    ingestor: Optional[Ingestor] = None,
    api_keys: Optional[ApiKeyStore] = None,
) -> FastAPI:
    app = FastAPI(title="NewFan AI-OCR Gateway", version="0.1.0")

    resolved_settings = settings or Settings.from_env()
    # CORS: web UI（別オリジン）から Bearer 認証で叩けるようにする（§6.1 は同一 API 前提だが
    # ブラウザ SPA は cross-origin。cookie 不使用のため allow_credentials は不要）。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved_settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.state.settings = resolved_settings
    app.state.repo = repo or InMemoryRepository()
    app.state.queue = queue or InMemoryQueue()
    app.state.orchestrator = orchestrator or FakeOrchestratorClient()
    app.state.api_keys = api_keys or InMemoryApiKeyStore({})
    app.state.idempotency = {}
    if ingestor is not None:
        app.state.ingestor = ingestor
    # ingestor 未注入時は本番構成（newfan_ingest）を遅延構築する。
    elif not hasattr(app.state, "ingestor"):
        app.state.ingestor = _default_ingestor(app.state.settings)

    @app.middleware("http")
    async def add_request_id(request: Request, call_next: Any) -> Any:
        rid = request.headers.get("X-Request-Id") or new_id("request")
        token = request_id_var.set(rid)
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers["X-Request-Id"] = rid
        return response

    app.add_exception_handler(ApiError, api_error_handler)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=500, content=error_body("E2000", "内部エラー", {}))

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(router)
    return app


def _default_ingestor(settings: Settings) -> Ingestor:
    # rasterizer は pypdfium2（runtime extra）。ObjectStore は S3_BUCKET 設定時 S3、無ければ Local。
    from newfan_ingest import IngestService
    from newfan_ingest.rasterize import PdfiumRasterizer

    if settings.s3_bucket:
        from newfan_ingest.storage import S3ObjectStore

        store: Any = S3ObjectStore(settings.s3_bucket, kms_key_id=settings.s3_kms_key_id)
    else:
        from newfan_ingest.storage import LocalObjectStore

        store = LocalObjectStore(settings.storage_root)
    return IngestService(store, PdfiumRasterizer())
