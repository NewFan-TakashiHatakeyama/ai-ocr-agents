# mypy: ignore-errors
"""Redis Streams consumer（export-svc, §9）。runtime 依存（redis）。"""

from __future__ import annotations

import json
from typing import Any


class RedisStreamConsumer:
    def __init__(self, url: str, stream: str, group: str, consumer: str) -> None:
        import redis

        self._redis = redis.Redis.from_url(url)
        self._stream = stream
        self._group = group
        self._consumer = consumer
        try:
            self._redis.xgroup_create(stream, group, id="0", mkstream=True)
        except Exception:  # noqa: BLE001 - 既存グループは無視
            pass

    def consume(self, *, count: int = 1, block_ms: int = 1000) -> list[tuple[str, dict[str, Any]]]:
        resp = self._redis.xreadgroup(
            self._group, self._consumer, {self._stream: ">"}, count=count, block=block_ms
        )
        out: list[tuple[str, dict[str, Any]]] = []
        for _stream, messages in resp or []:
            for msg_id, data in messages:
                payload = json.loads(data[b"payload"])
                out.append((msg_id.decode() if isinstance(msg_id, bytes) else msg_id, payload))
        return out

    def ack(self, message_id: str) -> None:
        self._redis.xack(self._stream, self._group, message_id)
