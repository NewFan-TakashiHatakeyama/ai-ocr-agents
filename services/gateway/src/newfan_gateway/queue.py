"""ジョブキュー抽象（§9, DD-05）。既定 Redis Streams / SQS 切替可。テストは InMemory。"""

from __future__ import annotations

from typing import Any, Protocol


class Queue(Protocol):
    def enqueue(self, stream: str, message: dict[str, Any]) -> None:
        """stream（q.extract 等）へメッセージ投入。本体は DB 参照（キューに業務データを載せない, §9）。"""
        ...


class InMemoryQueue:
    def __init__(self) -> None:
        self.messages: list[tuple[str, dict[str, Any]]] = []

    def enqueue(self, stream: str, message: dict[str, Any]) -> None:
        self.messages.append((stream, message))
