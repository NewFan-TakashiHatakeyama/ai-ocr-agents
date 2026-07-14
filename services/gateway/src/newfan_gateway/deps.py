"""FastAPI 依存（app.state から各実装を取り出す）。"""

from __future__ import annotations

from typing import Callable, Optional

from fastapi import Depends, Header, Request

from newfan_gateway.admin import AdminRepository
from newfan_gateway.auth import Principal, check_min_role, decode_principal
from newfan_gateway.config import Settings
from newfan_gateway.ports import Ingestor, OrchestratorClient
from newfan_gateway.queue import Queue
from newfan_gateway.repository import Repository


def get_settings(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def get_repo(request: Request) -> Repository:
    return request.app.state.repo  # type: ignore[no-any-return]


def get_queue(request: Request) -> Queue:
    return request.app.state.queue  # type: ignore[no-any-return]


def get_ingestor(request: Request) -> Ingestor:
    return request.app.state.ingestor  # type: ignore[no-any-return]


def get_orchestrator(request: Request) -> OrchestratorClient:
    return request.app.state.orchestrator  # type: ignore[no-any-return]


def get_admin(request: Request) -> AdminRepository:
    return request.app.state.admin  # type: ignore[no-any-return]


def get_principal(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    x_api_key: Optional[str] = Header(default=None),
) -> Principal:
    settings: Settings = request.app.state.settings
    return decode_principal(
        authorization=authorization,
        api_key=x_api_key,
        jwt_secret=settings.jwt_secret,
        jwt_alg=settings.jwt_alg,
        api_key_store=request.app.state.api_keys,
    )


def require_role(min_role: str) -> Callable[[Principal], Principal]:
    def _dep(principal: Principal = Depends(get_principal)) -> Principal:
        check_min_role(principal, min_role)
        return principal

    return _dep
