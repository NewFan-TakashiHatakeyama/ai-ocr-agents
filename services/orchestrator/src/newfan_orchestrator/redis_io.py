# mypy: ignore-errors
"""Redis Streams の producer/consumer（本番, §9）。runtime 依存（redis）。"""

from __future__ import annotations

import json
from typing import Any


class RedisQueue:
    """producer。XADD で enqueue（本体は DB 参照, §9）。"""

    def __init__(self, url: str) -> None:
        import redis

        self._redis = redis.Redis.from_url(url)

    def enqueue(self, stream: str, message: dict[str, Any]) -> None:
        self._redis.xadd(stream, {"payload": json.dumps(message)})


class RedisStreamConsumer:
    """consumer group ベースの消費（§9: Consumer Group=サービス名）。"""

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
