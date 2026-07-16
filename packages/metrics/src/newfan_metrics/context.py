"""テナントを実行文脈から拾う（§12.1 の {tenant} ラベル用）。

LLMAdapter や webhook のような低レイヤは tenant_id を引数で受け取っていない。
全ての呼び出しに引数を足して回ると署名が広く壊れるため、worker/リクエストの入口で
一度設定して、計装側が読む。

計測のためだけの仕組みなので、業務ロジックはここを読んではいけない
（テナント判定を contextvar に頼ると、設定漏れが権限バグになる）。
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

_tenant: ContextVar[str] = ContextVar("newfan_metrics_tenant", default="unknown")


def current_tenant() -> str:
    return _tenant.get()


@contextmanager
def tenant_scope(tenant_id: str) -> Iterator[None]:
    token = _tenant.set(tenant_id or "unknown")
    try:
        yield
    finally:
        _tenant.reset(token)
