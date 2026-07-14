# mypy: ignore-errors
"""E5Embedder（本番）: intfloat/multilingual-e5-small（DD-06）。

embeddings extra（sentence-transformers）が必要。384次元、normalize 済みを返す。
顧客修正データを外部APIへ出さないローカル推論（DD-06）。
"""

from __future__ import annotations

from newfan_memory.embedding import EMBED_DIM


class E5Embedder:
    def __init__(self, model_name: str = "intfloat/multilingual-e5-small") -> None:
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(model_name)
        self.dim = EMBED_DIM

    def embed(self, text: str) -> list[float]:
        # e5 は query:/passage: の prefix を text 側で付与済みとする（embedding.py 参照）
        vec = self._model.encode(text, normalize_embeddings=True)
        return [float(x) for x in vec]
