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


def test_create_rule_roundtrips_llm_hint() -> None:
    """PgAdminRepository.create_rule（③ llm_hint オーサリング）の実 DDL 回帰。

    敵対的レビュー確定所見: 本経路は InMemory テストしか無く、tenant_rules の
    列・CHECK 制約・RLS との整合が自動検証されていなかった。
    """
    from newfan_gateway.db import PgAdminRepository
    from newfan_gateway.records import RuleRecord

    admin = PgAdminRepository(_DSN)  # type: ignore[arg-type]
    tenant = "ten_test"
    rid = f"rul_{uuid.uuid4().hex[:12]}"
    with admin._engine.begin() as c:  # noqa: SLF001 - テスト用の前提データ投入
        from sqlalchemy import text

        c.execute(
            text("INSERT INTO tenants (id, name) VALUES (:i,:n) ON CONFLICT (id) DO NOTHING"),
            {"i": tenant, "n": "test"},
        )
    try:
        created = admin.create_rule(
            RuleRecord(
                id=rid, tenant_id=tenant, doc_type="invoice", field_name="issuer_name",
                rule_type="llm_hint",
                rule_json={"hint_text": "取引先名は右上の会社名を優先し敬称は除去", "description": "取り違え対策"},
                status="draft", created_by="sato",
            )
        )
        assert created is not None and created.id == rid
        got = admin.get_rule(tenant, rid)
        assert got is not None
        assert got.rule_type == "llm_hint"
        assert got.status == "draft"
        assert got.rule_json["hint_text"].startswith("取引先名")
        assert got.created_by == "sato"
        # 別テナントからは見えない（RLS/tenant フィルタ）
        assert admin.get_rule("ten_other", rid) is None
    finally:
        from sqlalchemy import text

        with admin._engine.begin() as c:  # noqa: SLF001
            c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant})
            c.execute(text("DELETE FROM tenant_rules WHERE id=:i"), {"i": rid})


def test_get_schema_by_id_matches_real_ddl() -> None:
    """get_schema_by_id が実 DDL（field_schemas に updated_at 無し）と整合すること。

    存在しない列を SELECT すると InMemory テストは通るのに本番だけ UndefinedColumn →
    抽出 API が 500 になる（実 AWS で発生・correction_logs の note 事故と同型）。
    """
    from newfan_gateway.db import PgAdminRepository
    from newfan_gateway.records import SchemaFieldDef

    admin = PgAdminRepository(_DSN)  # type: ignore[arg-type]
    tenant = "ten_test"
    with admin._engine.begin() as c:  # noqa: SLF001 - テスト用の前提データ投入
        from sqlalchemy import text

        c.execute(
            text("INSERT INTO tenants (id, name) VALUES (:i,:n) ON CONFLICT (id) DO NOTHING"),
            {"i": tenant, "n": "test"},
        )
    rec = admin.put_schema(
        tenant, f"ddl_probe_{uuid.uuid4().hex[:8]}",
        [SchemaFieldDef(name="total_amount", label="合計", type="money_jpy", required=True, critical=True)],
    )
    try:
        got = admin.get_schema_by_id(tenant, rec.id)
        assert got is not None and got.id == rec.id and got.doc_type == rec.doc_type
        assert got.fields[0].name == "total_amount"
        # 0007 の新列も SELECT に載っていること（列追加だけして SELECT を直し忘れると、
        # 値は書けているのに読めない＝UI 上は「保存したのに消えた」になる）
        assert got.exclude_regions == []
        assert got.source_page_count is None
        assert admin.get_schema_by_id(tenant, "sch_nonexistent") is None
        assert admin.get_schema_by_id("ten_other", rec.id) is None  # テナント境界
    finally:
        from sqlalchemy import text

        with admin._engine.begin() as c:  # noqa: SLF001
            c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant})
            c.execute(text("DELETE FROM field_schemas WHERE id=:i"), {"i": rec.id})


def test_pg_put_schema_legacy_put_inherits_exclude_regions() -> None:
    """exclude_regions の引き継ぎが**実 Pg の SQL で**成立すること（設計 §4.4 / C22）。

    InMemory の同名ユニットテストは ``db.py`` の SQL を 1 行も通らない。引き継ぎ元の
    SELECT から ``ORDER BY version DESC`` を落とすと「v1 の設定が復活して v2 以降が
    消える」という事故になるが、それは Pg でしか再現しない。旧 UI 相当の legacy 呼び
    出し（キーワード引数なし）を複数回はさんで、常に**最新版**から引き継ぐことを見る。
    """
    from sqlalchemy import text

    from newfan_gateway.db import PgAdminRepository
    from newfan_gateway.records import SchemaFieldDef

    admin = PgAdminRepository(_DSN)  # type: ignore[arg-type]
    tenant = "ten_test"
    other_tenant = "ten_test_other"
    doc_type = f"inherit_probe_{uuid.uuid4().hex[:8]}"
    other_doc_type = f"{doc_type}_other"

    r1 = {"page": 1, "rect": [0.10, 0.10, 0.20, 0.20], "label": "v1"}
    r2 = {"page": None, "rect": [0.80, 0.02, 0.98, 0.14], "label": "v2"}
    r_other = {"page": 1, "rect": [0.50, 0.50, 0.60, 0.60], "label": "other"}
    fields = [SchemaFieldDef(name="total_amount", type="money_jpy")]

    with admin._engine.begin() as c:  # noqa: SLF001 - テスト用の前提データ投入
        for t in (tenant, other_tenant):
            c.execute(
                text("INSERT INTO tenants (id, name) VALUES (:i,:n) ON CONFLICT (id) DO NOTHING"),
                {"i": t, "n": "test"},
            )

    made: list[str] = []
    try:
        # 混入源: 別 tenant / 別 doc_type にも版を作っておく
        made.append(
            admin.put_schema(
                other_tenant, doc_type, fields, exclude_regions=[r_other], source_page_count=9
            ).id
        )
        made.append(
            admin.put_schema(
                tenant, other_doc_type, fields, exclude_regions=[r_other], source_page_count=9
            ).id
        )

        v1 = admin.put_schema(tenant, doc_type, fields, exclude_regions=[r1], source_page_count=2)
        made.append(v1.id)
        v2 = admin.put_schema(tenant, doc_type, fields, exclude_regions=[r2])
        made.append(v2.id)
        # source_page_count も引き継がれる（v2 は値を送っていない）
        assert v2.source_page_count == 2

        # ③ 旧 UI 相当（キーワード引数なし）→ **v1 ではなく v2** を引き継ぐ
        v3 = admin.put_schema(tenant, doc_type, fields)
        made.append(v3.id)
        assert v3.exclude_regions[0].label == "v2", "最新版ではなく v1 から引き継いでいる"
        assert v3.source_page_count == 2
        # PUT 応答は引数由来ではなく INSERT した確定値であること（C28）
        assert admin.get_schema_by_id(tenant, v3.id).exclude_regions == v3.exclude_regions

        # ④ さらに legacy で put しても保たれる
        v4 = admin.put_schema(tenant, doc_type, fields)
        made.append(v4.id)
        assert v4.exclude_regions[0].label == "v2"

        # ⑤ 明示 [] はクリア
        v5 = admin.put_schema(tenant, doc_type, fields, exclude_regions=[])
        made.append(v5.id)
        assert v5.exclude_regions == []
        assert admin.get_schema_by_id(tenant, v5.id).exclude_regions == []
        # クリア後の legacy put は空を引き継ぐ（v2 が復活しない）
        v6 = admin.put_schema(tenant, doc_type, fields)
        made.append(v6.id)
        assert v6.exclude_regions == []

        # ⑥ 別 tenant / 別 doc_type は混ざっていない
        assert admin.get_schema(other_tenant, doc_type).exclude_regions[0].label == "other"
        assert admin.get_schema(tenant, other_doc_type).exclude_regions[0].label == "other"

        # region キーを持たない field は JSONB にも region を書かない（§4.7）
        with admin._engine.begin() as c:  # noqa: SLF001
            c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant})
            raw = c.execute(
                text("SELECT fields FROM field_schemas WHERE id=:i"), {"i": v1.id}
            ).scalar_one()
        assert "region" not in raw[0]
    finally:
        with admin._engine.begin() as c:  # noqa: SLF001
            for t in (tenant, other_tenant):
                c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": t})
                for sid in made:
                    c.execute(text("DELETE FROM field_schemas WHERE id=:i"), {"i": sid})
