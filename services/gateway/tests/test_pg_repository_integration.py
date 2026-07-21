"""PgRepository を実 PostgreSQL に対して検証する（§7）。

これまで gateway のテストは InMemoryRepository しか通しておらず、ORM と DDL の乖離を
一切検出できなかった。実際に本番で
  - column "note" of relation "correction_logs" does not exist
  - 'CorrectionRecord' object has no attribute 'note'
の 2 段階で修正保存が 500 になり、学習ループ（DD-06/DD-07）の入口が壊れていた。

DATABASE_URL_TEST が設定されている時だけ動く（CI/ローカルは compose の postgres を指す）。
例: postgresql+psycopg://newfan:newfan@localhost:5433/newfan
"""

from __future__ import annotations

import os
import uuid

import pytest

pytest.importorskip("sqlalchemy", reason="PgRepository は runtime 依存")
pytest.importorskip("psycopg", reason="PgRepository は runtime 依存")

_DSN = os.environ.get("DATABASE_URL_TEST")
pytestmark = pytest.mark.skipif(not _DSN, reason="DATABASE_URL_TEST 未設定（実 DB が要る）")


@pytest.fixture
def repo():
    from newfan_gateway.db import PgRepository

    return PgRepository(_DSN)  # type: ignore[arg-type]


@pytest.fixture
def seeded(repo):
    """tenant/document/run を実 DB に用意して id を返す。"""
    from newfan_gateway.records import DocumentRecord, PageRecord, RunRecord
    from sqlalchemy import text

    tenant = "ten_test"
    doc_id = f"doc_{uuid.uuid4().hex[:12]}"
    run_id = f"run_{uuid.uuid4().hex[:12]}"

    with repo._engine.begin() as c:  # noqa: SLF001 - テスト用の前提データ投入
        c.execute(
            text("INSERT INTO tenants (id, name) VALUES (:i,:n) ON CONFLICT (id) DO NOTHING"),
            {"i": tenant, "n": "test"},
        )
    repo.create_document(
        DocumentRecord(
            id=doc_id, tenant_id=tenant, storage_uri="s3://b/k", mime_type="image/png",
            page_count=1, doc_type="invoice", status="uploaded",
        ),
        [PageRecord(page_no=1, width=10, height=10, image_uri="s3://b/p1.png")],
    )
    repo.create_run(RunRecord(id=run_id, tenant_id=tenant, document_id=doc_id))
    yield tenant, doc_id, run_id


def test_add_corrections_persists_learning_keys(repo, seeded) -> None:
    """修正が実 DB に入り、学習ループが使う列まで保存されること。"""
    from newfan_gateway.records import CorrectionRecord
    from sqlalchemy import text

    tenant, doc_id, run_id = seeded
    cid = f"correction_{uuid.uuid4().hex[:12]}"
    repo.add_corrections(
        [
            CorrectionRecord(
                id=cid, tenant_id=tenant, document_id=doc_id, run_id=run_id,
                field_name="取引先名",
                original_value="株式会社エイ化ーエム",
                corrected_value="株式会社エイビーエム",
                doc_type="invoice",
                supplier_key="わくわく物産株式会社",
                context="株式会社エイ化ーエム 総務部",
                reviewer_id="sato",
            )
        ]
    )

    with repo._engine.begin() as c:  # noqa: SLF001
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant})
        row = c.execute(
            text(
                "SELECT field_name, original_value, corrected_value, doc_type, "
                "supplier_key, context, reviewer_id FROM correction_logs WHERE id = :i"
            ),
            {"i": cid},
        ).first()

    assert row is not None, "修正が保存されていない"
    assert row[0] == "取引先名"
    assert row[2] == "株式会社エイビーエム"
    # learn ノードが memory へ渡す値。ここが NULL だと修正が次回の抽出に効かない。
    assert row[3] == "invoice"
    assert row[4] == "わくわく物産株式会社"
    assert row[5] == "株式会社エイ化ーエム 総務部"
    assert row[6] == "sato"
