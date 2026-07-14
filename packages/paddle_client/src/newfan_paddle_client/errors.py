from __future__ import annotations

from typing import Optional


class PaddleServingError(Exception):
    """サービング呼出しの失敗（HTTP エラー・エンベロープ error_code 非0）。

    設計書 §10 のページ単位リトライ / E2001 判定はこの例外を捕捉して行う。
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        error_code: Optional[int] = None,
        endpoint: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.endpoint = endpoint
