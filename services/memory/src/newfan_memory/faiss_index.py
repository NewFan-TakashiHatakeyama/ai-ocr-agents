# mypy: ignore-errors
"""FaissIndex（本番）: テナント別 IndexFlatIP（DD-07, §5.8.3）。

faiss extra が必要。IndexFlatIP + IDMap で vector_id を保持。正規化ベクトルで cosine。
インデックスは S3/PVC へ非同期スナップショット、破損時は DB（correction_logs）から再構築（冪等）。
"""

from __future__ import annotations

from newfan_memory.embedding import EMBED_DIM


class FaissIndex:
    def __init__(self, dim: int = EMBED_DIM) -> None:
        import faiss
        import numpy as np

        self._np = np
        self._index = faiss.IndexIDMap(faiss.IndexFlatIP(dim))
        self._dim = dim

    def add(self, vector_id: int, vector: list[float]) -> None:
        v = self._np.asarray([vector], dtype="float32")
        ids = self._np.asarray([vector_id], dtype="int64")
        self._index.add_with_ids(v, ids)

    def search(self, vector: list[float], top_k: int) -> list[tuple[int, float]]:
        v = self._np.asarray([vector], dtype="float32")
        scores, ids = self._index.search(v, top_k)
        return [
            (int(i), float(s))
            for i, s in zip(ids[0], scores[0])
            if int(i) != -1
        ]

    def __len__(self) -> int:
        return int(self._index.ntotal)
