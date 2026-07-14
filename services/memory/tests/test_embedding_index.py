import math

from newfan_memory import HashingEmbedder, InMemoryIndex, embedding_key, passage_text, query_text
from newfan_memory.embedding import EMBED_DIM


def test_embedder_dim_and_normalized() -> None:
    e = HashingEmbedder()
    v = e.embed("query: doc:invoice|f:total|v:128000")
    assert len(v) == EMBED_DIM
    assert math.isclose(math.sqrt(sum(x * x for x in v)), 1.0, abs_tol=1e-9)


def test_embedder_deterministic_across_calls() -> None:
    e = HashingEmbedder()
    assert e.embed("passage: abc") == e.embed("passage: abc")


def test_query_passage_prefix_invariant() -> None:
    # e5 prefix は語彙判定で無視 → 同一キーの query/passage は同値
    e = HashingEmbedder()
    key = embedding_key(doc_type="invoice", supplier="A社", field="total", value_raw="1", context="x")
    assert e.embed(query_text(key)) == e.embed(passage_text(key))


def test_similar_keys_more_similar() -> None:
    e = HashingEmbedder()

    def dot(a: list[float], b: list[float]) -> float:
        return sum(x * y for x, y in zip(a, b))

    base = e.embed(embedding_key(doc_type="invoice", supplier="A社", field="total", value_raw="128,OOO", context="御請求金額"))
    similar = e.embed(embedding_key(doc_type="invoice", supplier="A社", field="total", value_raw="256,OOO", context="御請求金額"))
    different = e.embed(embedding_key(doc_type="order", supplier="Z社", field="qty", value_raw="3", context="数量"))
    assert dot(base, similar) > dot(base, different)


def test_index_topk_order() -> None:
    e = HashingEmbedder()
    idx = InMemoryIndex()
    idx.add(1, e.embed("passage: alpha beta gamma"))
    idx.add(2, e.embed("passage: totally different words here"))
    hits = idx.search(e.embed("query: alpha beta gamma"), top_k=2)
    assert hits[0][0] == 1  # 最も近いのは同一テキスト
    assert hits[0][1] > hits[1][1]
