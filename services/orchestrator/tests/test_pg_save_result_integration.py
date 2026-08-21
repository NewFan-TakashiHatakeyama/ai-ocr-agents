# mypy: ignore-errors
"""PgContextStore.save_result の実 DDL 統合テスト（敵対的レビュー確定所見の固定）。

固定する契約:
1. correction / validation が実際に永続化される（f0bc443 で直した欠落の再発防止。
   InMemory はモデルを丸ごと保持するため、この欠落はローカルでは原理的に検出できない）
2. 再配信の再実行（同 run_id への再保存）は行全体を書き直す（キメラ行防止）
3. 人手確定（corrected/approved）は機械の再抽出（pending/auto）で巻き戻らない
4. confirmed の run/document 状態は needs_review へ巻き戻らない

DATABASE_URL_TEST が設定されている時だけ動く。
"""

from __future__ import annotations

import os
import uuid

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("psycopg")

_DSN = os.environ.get("DATABASE_URL_TEST")
pytestmark = pytest.mark.skipif(not _DSN, reason="DATABASE_URL_TEST 未設定（実 DB が要る）")

from newfan_schemas import ExtractedField, ReviewStatus  # noqa: E402

TENANT = "ten_savepin"


def _field(name, raw, norm, *, rs=ReviewStatus.AUTO, correction=None, validation=None,
           quote=None, span_ids=(1,)):
    return ExtractedField(
        name=name, value_raw=raw, value_normalized=norm, span_ids=list(span_ids),
        page=1, source_quote=quote or raw, confidence=0.9, grounding_score=1.0,
        review_status=rs, correction=correction, validation=validation,
    )


@pytest.fixture
def env():
    from sqlalchemy import create_engine, text

    from newfan_orchestrator.pg_persistence import PgContextStore

    owner = create_engine(_DSN, future=True)
    doc_id = f"doc_{uuid.uuid4().hex[:12]}"
    run_id = f"run_{uuid.uuid4().hex[:12]}"
    with owner.begin() as c:
        c.execute(text("INSERT INTO tenants (id,name) VALUES (:t,'x') ON CONFLICT DO NOTHING"),
                  {"t": TENANT})
        c.execute(text(
            "INSERT INTO documents (id, tenant_id, storage_uri, mime_type, page_count, status)"
            " VALUES (:d,:t,'s3://x','image/png',1,'uploaded')"), {"d": doc_id, "t": TENANT})
        c.execute(text(
            "INSERT INTO extraction_runs (id, tenant_id, document_id, status, engine_versions)"
            " VALUES (:r,:t,:d,'processing','{}'::jsonb)"), {"r": run_id, "t": TENANT, "d": doc_id})
    store = PgContextStore(_DSN)
    yield owner, store, doc_id, run_id
    with owner.begin() as c:
        c.execute(text("DELETE FROM extraction_fields WHERE run_id=:r"), {"r": run_id})
        c.execute(text("DELETE FROM extraction_tables WHERE run_id=:r"), {"r": run_id})
        c.execute(text("DELETE FROM extraction_runs WHERE id=:r"), {"r": run_id})
        c.execute(text("DELETE FROM documents WHERE id=:d"), {"d": doc_id})
        c.execute(text("DELETE FROM tenants WHERE id=:t"), {"t": TENANT})


def _row(owner, run_id, name):
    from sqlalchemy import text

    with owner.begin() as c:
        return c.execute(text(
            "SELECT value_raw, final_value, review_status, correction, validation, source_quote"
            " FROM extraction_fields WHERE run_id=:r AND field_name=:f"),
            {"r": run_id, "f": name}).mappings().first()


def test_correctionとvalidationが実DDLへ永続化される(env) -> None:
    owner, store, _doc, run_id = env
    store.save_result(
        TENANT, run_id,
        fields=[_field("total", "136,998", "136998",
                       correction={"applied": True, "from": "136,998", "to": "136998"},
                       validation={"ok": True})],
        tables=[], review_items=[], status="needs_review",
    )
    r = _row(owner, run_id, "total")
    assert r["correction"] == {"applied": True, "from": "136,998", "to": "136998"}
    assert r["validation"] == {"ok": True}


def test_再保存は行全体を書き直しキメラ行にならない(env) -> None:
    owner, store, _doc, run_id = env
    store.save_result(TENANT, run_id, fields=[_field("total", "128,000", "128000", quote="旧引用")],
                      tables=[], review_items=[], status="needs_review")
    # 再配信の再実行: OCR 揺れで raw も引用も変わる
    store.save_result(
        TENANT, run_id,
        fields=[_field("total", "178,000", "178000", quote="新引用",
                       correction={"applied": True, "from": "178,000", "to": "178000"})],
        tables=[], review_items=[], status="needs_review",
    )
    r = _row(owner, run_id, "total")
    assert r["value_raw"] == "178,000"  # 旧 raw が残ると correction と矛盾するキメラ行
    assert r["source_quote"] == "新引用"
    assert r["correction"]["from"] == "178,000"


def test_人手確定は機械の再抽出で巻き戻らない(env) -> None:
    owner, store, doc_id, run_id = env
    from sqlalchemy import text

    # 1回目: needs_review → 人手確定（finalize 相当の corrected 保存）
    store.save_result(TENANT, run_id, fields=[_field("total", "136,998", "136998")],
                      tables=[], review_items=[], status="needs_review")
    store.save_result(TENANT, run_id,
                      fields=[_field("total", "136,998", "999999", rs=ReviewStatus.CORRECTED)],
                      tables=[], review_items=[], status="confirmed")
    # 再配信の再実行が needs_review/auto を書こうとする
    store.save_result(TENANT, run_id, fields=[_field("total", "136,998", "136998")],
                      tables=[], review_items=[], status="needs_review")

    r = _row(owner, run_id, "total")
    assert r["review_status"] == "corrected"  # pending/auto へ巻き戻らない
    assert r["final_value"] == "999999"  # 人手確定値が保持される
    with owner.begin() as c:
        st = c.execute(text("SELECT status FROM extraction_runs WHERE id=:r"), {"r": run_id}).scalar()
        dst = c.execute(text("SELECT status FROM documents WHERE id=:d"), {"d": doc_id}).scalar()
    assert st == "confirmed"  # run も confirmed のまま
    assert dst == "confirmed"  # document も追従して巻き戻らない
