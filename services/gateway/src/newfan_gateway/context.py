from __future__ import annotations

from contextvars import ContextVar

# 相関ID（§6.1 X-Request-Id）。ミドルウェアが設定し、エラー整形で参照する。
request_id_var: ContextVar[str] = ContextVar("request_id", default="")
