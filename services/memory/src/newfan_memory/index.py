"""ベクタ検索（§5.8.3, DD-07）。

本番は faiss IndexFlatIP（テナント別）。テスト/dev は InMemoryIndex（純Python, 同義）。
正規化ベクトルの内積 = cosine。正本は PostgreSQL、index は再構築可能な派生物（DD-07）。
"""

from __future__ import annotations

from typing import Protocol


class VectorIndex(Protocol):
    def add(self, vector_id: int, vector: list[float]) -> None: ...
    def search(self, vector: list[float], top_k: int) -> list[tuple[int, float]]: ...
    def __len__(self) -> int: ...


class InMemoryIndex:
    """IndexFlatIP 相当（純Python）。ベクトルは正規化済み前提（内積=cosine）。"""

    def __init__(self) -> None:
        self._ids: list[int] = []
        self._vecs: list[list[float]] = []

    def add(self, vector_id: int, vector: list[float]) -> None:
        self._ids.append(vector_id)
        self._vecs.append(list(vector))

    def search(self, vector: list[float], top_k: int) -> list[tuple[int, float]]:
        scored = [
            (vid, sum(a * b for a, b in zip(vec, vector)))
            for vid, vec in zip(self._ids, self._vecs)
        ]
        scored.sort(key=lambda t: t[1], reverse=True)
        return scored[:top_k]

    def __len__(self) -> int:
        return len(self._ids)
