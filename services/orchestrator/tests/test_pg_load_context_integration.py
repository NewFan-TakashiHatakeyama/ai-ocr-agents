# mypy: ignore-errors
"""PgContextStore.load_context の実 DDL 統合テスト（設計 §8 統合 / C15）。

`load_context` の SELECT は、これまでどのテストにも守られていなかった
（`test_pg_context_schema.py` は EMPTY_SCHEMA が FieldSchema として妥当かを見るだけの
単体テストで、DSN も load_context も使わない）。列を追加して migration を流し忘れると
`SELECT ..., exclude_regions` が UndefinedColumn で**全 run が失敗**するが、CI では
検出できない——`correction_logs.note` で実際に起きた事故と同型である。

固定する契約:
1. field_schemas → LoadedContext → db_nodes → state の実 Pg 貫通
2. **schema dict に exclude_regions / source_page_count を混ぜない**
   （schema は make_kie_extract がそのまま json.dumps でプロンプトへ埋めるため、
   混ぜると領域設定の有無で LLM 出力が変わる）
3. schema_id が NULL（テンプレートレス run）では [] / None になる

DATABASE_URL_TEST が設定されている時だけ動く。
"""

from __future__ import annotations

import json
import os
import uuid

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("psycopg")

_DSN = os.environ.get("DATABASE_URL_TEST")
pytestmark = pytest.mark.skipif(not _DSN, reason="DATABASE_URL_TEST 未設定（実 DB が要る）")

TENANT = "ten_loadctx"

R_TITLE = {"page": 1, "rect": [0.30, 0.02, 0.72, 0.09]}
X_STAMP = {"page": None, "rect": [0.82, 0.02, 0.98, 0.14], "label": "社印"}


@pytest.fixture
def env():
    """tenant / document / pages / field_schemas / extraction_runs を実 DB に用意する。

    scripts/e2e_real.py の INSERT 形に合わせ、**pages の寸法と field_schemas の
    新列まで**投入する（寸法欠落は別テストで扱う）。
    """
    from sqlalchemy import create_engine, text

    from newfan_orchestrator.pg_persistence import PgContextStore

    owner = create_engine(_DSN, future=True)
    doc_id = f"doc_{uuid.uuid4().hex[:12]}"
    schema_id = f"sch_{uuid.uuid4().hex[:12]}"
    run_typed = f"run_{uuid.uuid4().hex[:12]}"
    run_bare = f"run_{uuid.uuid4().hex[:12]}"

    with owner.begin() as c:
        c.execute(
            text("INSERT INTO tenants (id,name) VALUES (:t,'x') ON CONFLICT DO NOTHING"),
            {"t": TENANT},
        )
        c.execute(
            text(
                "INSERT INTO documents (id, tenant_id, storage_uri, mime_type, page_count, status)"
                " VALUES (:d,:t,'s3://x','image/png',2,'uploaded')"
            ),
            {"d": doc_id, "t": TENANT},
        )
        for page_no, (w, h) in enumerate([(1000, 1400), (1000, 1400)], start=1):
            c.execute(
                text(
                    "INSERT INTO pages (id, tenant_id, document_id, page_no, width, height, image_uri)"
                    " VALUES (:i,:t,:d,:p,:w,:h,:u)"
                ),
                {
                    "i": f"pg_{doc_id}_{page_no}", "t": TENANT, "d": doc_id, "p": page_no,
                    "w": w, "h": h, "u": f"s3://x/p{page_no}.png",
                },
            )
        c.execute(
            text(
                "INSERT INTO field_schemas"
                " (id, tenant_id, doc_type, version, fields, exclude_regions, source_page_count)"
                " VALUES (:i,:t,'invoice',1, CAST(:f AS jsonb), CAST(:x AS jsonb), 2)"
            ),
            {
                "i": schema_id,
                "t": TENANT,
                "f": json.dumps(
                    [{"name": "title", "type": "string", "region": R_TITLE}], ensure_ascii=False
                ),
                "x": json.dumps([X_STAMP], ensure_ascii=False),
            },
        )
        for rid, sid in ((run_typed, schema_id), (run_bare, None)):
            c.execute(
                text(
                    "INSERT INTO extraction_runs"
                    " (id, tenant_id, document_id, schema_id, status, engine_versions)"
                    " VALUES (:r,:t,:d,:s,'processing','{}'::jsonb)"
                ),
                {"r": rid, "t": TENANT, "d": doc_id, "s": sid},
            )

    yield PgContextStore(_DSN), doc_id, run_typed, run_bare

    with owner.begin() as c:
        c.execute(
            text("DELETE FROM extraction_runs WHERE id = ANY(:r)"),
            {"r": [run_typed, run_bare]},
        )
        c.execute(text("DELETE FROM field_schemas WHERE id=:i"), {"i": schema_id})
        c.execute(text("DELETE FROM pages WHERE document_id=:d"), {"d": doc_id})
        c.execute(text("DELETE FROM documents WHERE id=:d"), {"d": doc_id})
        c.execute(text("DELETE FROM tenants WHERE id=:t"), {"t": TENANT})


def test_load_context_reads_exclude_regions_and_source_page_count(env) -> None:
    """新列が実 DDL から読めること（列追加と SELECT 追加の同一コミット保証）。"""
    store, _doc, run_typed, _bare = env
    ctx = store.load_context(TENANT, run_typed)
    assert ctx is not None
    assert ctx.exclude_regions == [X_STAMP]
    assert ctx.source_page_count == 2
    # fields の region はスキーマ側に残る（プロンプト除去は llm_nodes の責務）
    assert ctx.schema["fields"][0]["region"] == R_TITLE
    assert [p["width"] for p in ctx.pages] == [1000, 1000]


def test_schema_dict_has_no_region_metadata(env) -> None:
    """schema dict に exclude / source_page_count を混ぜない（プロンプト汚染防止）。

    schema は make_kie_extract がそのまま json.dumps でプロンプトへ埋めるため、
    ここに座標やページ数が入ると「除外領域を設定しただけで抽出結果が変わる」。
    """
    store, _doc, run_typed, _bare = env
    ctx = store.load_context(TENANT, run_typed)
    assert set(ctx.schema.keys()) == {"doc_type", "fields"}


def test_bare_run_gets_empty_regions(env) -> None:
    """schema_id が NULL（テンプレートレス run）では [] / None。"""
    store, _doc, _typed, run_bare = env
    ctx = store.load_context(TENANT, run_bare)
    assert ctx is not None
    assert ctx.exclude_regions == []
    assert ctx.source_page_count is None
    assert ctx.schema == {"doc_type": "", "fields": []}


def test_db_nodes_puts_regions_on_state(env) -> None:
    """load_context ノードが state のトップレベルへ積むこと（§4.6 の配線）。

    ここが抜けると confidence_gate_node は state しか見られないため、位置ガードの
    ページ数判定が実装不能になる（LoadedContext まで来ていても届かない）。
    """
    from newfan_orchestrator.db_nodes import make_load_context

    store, doc_id, run_typed, _bare = env
    out = make_load_context(store)({"tenant_id": TENANT, "run_id": run_typed})
    assert out["document_id"] == doc_id
    assert out["exclude_regions"] == [X_STAMP]
    assert out["source_page_count"] == 2
    assert "exclude_regions" not in out["schema"]
