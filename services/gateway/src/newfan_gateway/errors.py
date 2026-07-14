"""エラー形式・コード体系（詳細設計 §6.1 / §6.5）。"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import Request
from fastapi.responses import JSONResponse

from newfan_gateway.context import request_id_var

# §6.5 エラーコード → HTTP ステータス
ERROR_HTTP = {
    "E1001": 400,
    "E1002": 413,
    "E1003": 422,
    "E1005": 409,
    "E1006": 409,
    "E2000": 500,
    "E2001": 502,
    "E2002": 502,
    "E3001": 504,
    "E3002": 502,
    "E4001": 422,
    "E5001": 403,
    "E5002": 429,
}


class ApiError(Exception):
    def __init__(
        self, code: str, message: str, *, details: Optional[dict[str, Any]] = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.http_status = ERROR_HTTP.get(code, 400)


def error_body(code: str, message: str, details: dict[str, Any]) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details,
            "request_id": request_id_var.get(),
        }
    }


async def api_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApiError)
    return JSONResponse(
        status_code=exc.http_status,
        content=error_body(exc.code, exc.message, exc.details),
    )
