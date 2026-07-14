"""埋め込み（§5.8.2, DD-06）。

本番は intfloat/multilingual-e5-small（384次元, ローカル推論）。テスト/dev は
HashingEmbedder（ハッシュトリック）で決定論的に語彙類似を再現する。埋め込みキーは §5.8.2。
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol

EMBED_DIM = 384  # e5-small の次元（DD-06）


def embedding_key(
    *, doc_type: str, supplier: str, field: str, value_raw: str, context: str
) -> str:
    """§5.8.2 の埋め込みキー。"""
    return (
        f"doc:{doc_type}|sup:{supplier}|f:{field}|v:{value_raw}|ctx:{context[:200]}"
    )


def query_text(key: str) -> str:
    return f"query: {key}"


def passage_text(key: str) -> str:
    return f"passage: {key}"


class Embedder(Protocol):
    dim: int

    def embed(self, text: str) -> list[float]:
        """L2 正規化済みベクトルを返す（cosine = 内積）。"""
        ...


_E5_PREFIX = re.compile(r"^(query|passage):\s*")


def _tokens(text: str) -> list[str]:
    words = re.findall(r"\w+", text.lower())
    bigrams = [text[i : i + 2] for i in range(len(text) - 1)]
    return words + bigrams


def _l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0.0:
        return vec
    return [v / norm for v in vec]


class HashingEmbedder:
    """ハッシュトリックによる決定論埋め込み（テスト/dev）。

    語彙（単語＋文字bigram）を共有するテキスト同士は高い cosine を持つ。プロセス跨ぎで安定させる
    ため blake2b を使う（Python の hash() はプロセス毎に salt されるため不可）。
    """

    def __init__(self, dim: int = EMBED_DIM) -> None:
        self.dim = dim

    def embed(self, text: str) -> list[float]:
        # e5 の query:/passage: prefix は語彙類似の判定では無視する（query と passage を同値に）
        text = _E5_PREFIX.sub("", text)
        vec = [0.0] * self.dim
        for token in _tokens(text):
            h = hashlib.blake2b(token.encode("utf-8"), digest_size=16).digest()
            idx = int.from_bytes(h[:8], "big") % self.dim
            sign = 1.0 if (h[8] & 1) else -1.0
            vec[idx] += sign
        return _l2_normalize(vec)
