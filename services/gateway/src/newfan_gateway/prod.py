# mypy: ignore-errors
"""本番アダプタ: Redis Streams キューと orchestrator への内部 RPC（§9 / §4.4）。

RedisQueue は runtime extra（redis）が必要。HttpOrchestratorClient は httpx を使う。
resume は orchestrator が再開ジョブとして受け、ワーカーが invoke する（Web 内で長時間実行しない）。
"""

from __future__ import annotations

import json
from typing import Any, Optional

import httpx


class RedisQueue:
    def __init__(self, url: str) -> None:
        import redis

        self._redis = redis.Redis.from_url(url)

    def enqueue(self, stream: str, message: dict[str, Any]) -> None:
        # XADD。値は文字列化（本体は DB 参照, §9）
        self._redis.xadd(stream, {"payload": json.dumps(message)})


class HttpOrchestratorClient:
    def __init__(self, base_url: str, *, timeout: float = 10.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def resume(self, run_id: str, feedback: Optional[dict[str, Any]]) -> None:
        with httpx.Client(base_url=self._base_url, timeout=self._timeout) as client:
            resp = client.post(f"/internal/runs/{run_id}/resume", json={"feedback": feedback})
            resp.raise_for_status()
