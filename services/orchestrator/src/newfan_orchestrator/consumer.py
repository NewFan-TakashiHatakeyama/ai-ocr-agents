"""ジョブキュー消費（§9）。Redis Streams（本番）/ InMemory（テスト）。"""

from __future__ import annotations

from typing import Any, Protocol


class QueueConsumer(Protocol):
    def consume(self, *, count: int = 1, block_ms: int = 1000) -> list[tuple[str, dict[str, Any]]]:
        """未 ACK のメッセージを最大 count 件返す（(message_id, payload)）。"""
        ...

    def ack(self, message_id: str) -> None: ...


class InMemoryQueueConsumer:
    """テスト/dev 用。push で投入し、consume→ack で消費する。"""

    def __init__(self) -> None:
        self._pending: list[tuple[str, dict[str, Any]]] = []
        self._acked: set[str] = set()
        self._seq = 0

    def push(self, payload: dict[str, Any]) -> str:
        self._seq += 1
        mid = str(self._seq)
        self._pending.append((mid, payload))
        return mid

    def consume(self, *, count: int = 1, block_ms: int = 1000) -> list[tuple[str, dict[str, Any]]]:
        return [m for m in self._pending if m[0] not in self._acked][:count]

    def ack(self, message_id: str) -> None:
        self._acked.add(message_id)

    def pending_count(self) -> int:
        return len([m for m in self._pending if m[0] not in self._acked])
