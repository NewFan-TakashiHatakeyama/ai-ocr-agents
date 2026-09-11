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

import json
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


def test_pg_region_hint_fields_roundtrip_jsonb() -> None:
    """example_value / origin / created_at が fields JSONB を経由して戻ること。

    設計 region-field-add-and-hint-v2 §2.3。InMemory はモデルをそのまま持つので
    JSONB の直列化（``schema_fields_payload``）→ ``SchemaFieldDef.model_validate``
    の経路は実 Pg でしか通らない。3 項目のキーが無い旧 JSONB も読めることを併せて見る。
    """
    from sqlalchemy import text

    from newfan_gateway.db import PgAdminRepository
    from newfan_gateway.records import SchemaFieldDef
    from newfan_schemas import RegionRect

    admin = PgAdminRepository(_DSN)  # type: ignore[arg-type]
    tenant = "ten_test"
    doc_type = f"hint_probe_{uuid.uuid4().hex[:8]}"
    with admin._engine.begin() as c:  # noqa: SLF001 - テスト用の前提データ投入
        c.execute(
            text("INSERT INTO tenants (id, name) VALUES (:i,:n) ON CONFLICT (id) DO NOTHING"),
            {"i": tenant, "n": "test"},
        )
    hinted = RegionRect(
        page=1,
        rect=[0.30, 0.02, 0.72, 0.09],
        example_value="株式会社千曲川ホーム",
        origin="ghost",
        created_at="2026-09-11T00:00:00Z",
    )
    made: list[str] = []
    try:
        rec = admin.put_schema(
            tenant,
            doc_type,
            [
                SchemaFieldDef(name="issuer", type="string", region=hinted),
                SchemaFieldDef(
                    name="total", type="money_jpy",
                    region=RegionRect(page="last", rect=[0.65, 0.80, 0.95, 0.88]),
                ),
            ],
        )
        made.append(rec.id)
        for got in (admin.get_schema(tenant, doc_type), admin.get_schema_by_id(tenant, rec.id)):
            assert got is not None
            by_name = {f.name: f for f in got.fields}
            assert by_name["issuer"].region == hinted
            plain = by_name["total"].region
            assert plain is not None and plain.page == "last"
            assert (plain.example_value, plain.origin, plain.created_at) == (None, None, None)

        # 旧世代の JSONB（region に 3 項目のキー自体が無い）も読める
        with admin._engine.begin() as c:  # noqa: SLF001
            c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant})
            c.execute(
                text("UPDATE field_schemas SET fields = CAST(:f AS jsonb) WHERE id=:i"),
                {
                    "f": '[{"name": "issuer", "label": null, "type": "string", "required": false,'
                    ' "critical": false, "columns": null,'
                    ' "region": {"page": 1, "rect": [0.3, 0.02, 0.72, 0.09], "label": null}}]',
                    "i": rec.id,
                },
            )
        legacy = admin.get_schema_by_id(tenant, rec.id)
        assert legacy is not None
        region = legacy.fields[0].region
        assert region is not None and region.rect == [0.3, 0.02, 0.72, 0.09]
        assert (region.example_value, region.origin, region.created_at) == (None, None, None)
    finally:
        with admin._engine.begin() as c:  # noqa: SLF001
            c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant})
            for sid in made:
                c.execute(text("DELETE FROM field_schemas WHERE id=:i"), {"i": sid})


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


def test_pg_supersede_review_runs(repo, seeded) -> None:
    """needs_review の run だけを superseded へ落とす（実 Pg）。

    InMemory 版はモデル属性を書き換えるだけなので、SQL や RLS ヘルパの使い方の
    誤りを一切検出できない。実際に PgRepository._rls（セッションを yield する
    contextmanager）を PgAdminRepository._rls（接続に SET を撃つだけ）と同じ形で
    呼んで本番だけ 500 になる事故を起こしたため、この経路は Pg で守る。
    """
    from sqlalchemy import text

    from newfan_gateway.records import RunRecord

    tenant, doc_id, processing_run = seeded
    review_run = f"run_{uuid.uuid4().hex[:12]}"
    repo.create_run(
        RunRecord(id=review_run, tenant_id=tenant, document_id=doc_id, status="needs_review")
    )
    try:
        assert repo.supersede_review_runs(tenant, doc_id) == 1
        with repo._engine.begin() as c:  # noqa: SLF001 - 状態の直接確認
            c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant})
            rows = dict(
                c.execute(
                    text("SELECT id, status FROM extraction_runs WHERE document_id=:d"),
                    {"d": doc_id},
                ).all()
            )
        assert rows[review_run] == "superseded"
        assert rows[processing_run] == "processing"  # 実行中の run は触らない
        # 冪等（もう needs_review が無いので 0 件）
        assert repo.supersede_review_runs(tenant, doc_id) == 0
        # テナント境界
        assert repo.supersede_review_runs("ten_other", doc_id) == 0
    finally:
        with repo._engine.begin() as c:  # noqa: SLF001
            c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant})
            c.execute(text("DELETE FROM extraction_runs WHERE id=:i"), {"i": review_run})


def test_pg_get_latest_run_orders_by_start_time(repo, seeded) -> None:
    """複数 run がある帳票で **開始時刻が最新の** run を返すこと。

    実装は長く `ORDER BY id DESC` だった。run id は `run_` + ランダム uuid なので、
    これは「最新」ではなく実質ランダムに 1 本を選ぶ。帳票に run が 1 本しか無い間は
    表面化しないが、チャットの再抽出や supersede 付き再抽出で 2 本目ができた瞬間に
    検証画面が古い結果を表示し始める（実機で 4 回中 1 回再現した）。

    id の大小と時刻の順序を**逆**にした 2 本で、時刻が勝つことを確かめる。
    InMemory 実装は started_at で並べているのでこの差は Pg でしか出ない。
    """
    from sqlalchemy import text

    from newfan_gateway.records import RunRecord

    tenant, doc_id, _first = seeded
    older_but_bigger_id = "run_zzzz_old"
    newer_but_smaller_id = "run_aaaa_new"
    repo.create_run(RunRecord(id=older_but_bigger_id, tenant_id=tenant, document_id=doc_id))
    repo.create_run(RunRecord(id=newer_but_smaller_id, tenant_id=tenant, document_id=doc_id))
    try:
        with repo._engine.begin() as c:  # noqa: SLF001 - 開始時刻を明示的にずらす
            c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant})
            c.execute(
                text("UPDATE extraction_runs SET started_at = now() - interval '1 hour'"
                     " WHERE id = :i"),
                {"i": older_but_bigger_id},
            )
        latest = repo.get_latest_run(tenant, doc_id)
        assert latest is not None and latest.id == newer_but_smaller_id
    finally:
        with repo._engine.begin() as c:  # noqa: SLF001
            c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant})
            c.execute(
                text("DELETE FROM extraction_runs WHERE id = ANY(:i)"),
                {"i": [older_but_bigger_id, newer_but_smaller_id]},
            )


def test_pg_set_document_doc_type_はupdated_atを進めない(repo, seeded) -> None:
    """種別の書き戻しで documents.updated_at を触らないこと。

    updated_at の読み手は get_delete_blocker の「確定処理中の窓」判定だけで、
    set_document_status がそこを進めるのは status 遷移＝処理が動いた証拠だから。
    メタ情報の更新でそこを進めると「種別を直しただけで削除が stale_minutes 分
    ブロックされる」副作用が出る。ORM に updated_at が無いため、実 DB でしか見えない。
    """
    from sqlalchemy import text

    tenant, doc_id, _run = seeded

    def _row() -> tuple[str | None, object]:
        with repo._engine.begin() as c:  # noqa: SLF001
            c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant})
            return tuple(  # type: ignore[return-value]
                c.execute(
                    text("SELECT doc_type, updated_at FROM documents WHERE id = :d"),
                    {"d": doc_id},
                ).one()
            )

    before_type, before_updated = _row()
    # seeded の doc_type は "invoice"。同じ値だと「書き換わったか」を観測できない
    assert before_type == "invoice"
    repo.set_document_doc_type(tenant, doc_id, "quotation")
    after_type, after_updated = _row()
    assert after_type == "quotation"
    assert after_updated == before_updated


def test_pg_set_document_doc_type_は他テナントの行を書き換えない(repo, seeded) -> None:
    """RLS ポリシーは USING のみで WITH CHECK が無いため、WHERE 側が唯一の防御になる。"""
    from sqlalchemy import text

    tenant, doc_id, _run = seeded
    repo.set_document_doc_type("ten_other", doc_id, "quotation")
    with repo._engine.begin() as c:  # noqa: SLF001
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant})
        got = c.execute(
            text("SELECT doc_type FROM documents WHERE id = :d"), {"d": doc_id}
        ).scalar()
    assert got == "invoice"  # seeded のまま（他テナント指定では書き換わらない）


def test_pg_get_run_spans_は自テナントの行だけ返す(repo, seeded) -> None:
    """run_spans（0008）を実 DDL で読めること、他テナントからは空になること（設計 D12）。

    ローカル compose の接続ロールは所有者（RLS を素通り）なので、テナント境界は
    WHERE の tenant_id が唯一の防御。行が無いページは空配列（エラーにしない）。
    """
    from sqlalchemy import text

    tenant, _doc_id, run_id = seeded
    spans = [
        {"span_id": 1, "text": "株式会社千曲川ホーム", "bbox": [10, 20, 110, 40], "conf": 0.95},
        {"span_id": 2, "text": "御請求書", "bbox": None, "conf": 0.8},
    ]
    with repo._engine.begin() as c:  # noqa: SLF001 - orchestrator が書いた状態を再現
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant})
        c.execute(
            text(
                "INSERT INTO run_spans (run_id, tenant_id, page_no, spans)"
                " VALUES (:r, :t, 1, CAST(:s AS jsonb))"
            ),
            {"r": run_id, "t": tenant, "s": json.dumps(spans, ensure_ascii=False)},
        )
    try:
        assert repo.get_run_spans(tenant, run_id, 1) == spans
        assert repo.get_run_spans(tenant, run_id, 2) == []  # 行の無いページ
        assert repo.get_run_spans("ten_other", run_id, 1) == []  # テナント境界
    finally:
        with repo._engine.begin() as c:  # noqa: SLF001
            c.execute(text("DELETE FROM run_spans WHERE run_id = :r"), {"r": run_id})


def test_pg_legacy_reserved_field_name_does_not_break_reads() -> None:
    """検査導入前に保存された予約名（``__`` 始まり）の行があっても、読み出しは落ちない。

    敵対的レビューで実 Pg に再現された事故: 予約名の validator を読み出しモデルにも
    置いていたため、旧データ 1 行で ``list_schemas`` が **同テナントの健全な doc_type
    まで巻き込んで** ValidationError になった（GET /v1/schemas がテナント丸ごと 500、
    スキーマ管理画面もテンプレート化画面も開けず、DELETE /schemas は無いので SQL 以外に
    復旧手段が無い）。拒否は書き込み側（put_schema）に限り、読み出しは旧データを通す。
    """
    from sqlalchemy import text

    from newfan_gateway.db import PgAdminRepository
    from newfan_gateway.records import SchemaFieldDef

    admin = PgAdminRepository(_DSN)  # type: ignore[arg-type]
    tenant = "ten_test"
    legacy_type = f"legacy_reserved_{uuid.uuid4().hex[:8]}"
    healthy_type = f"healthy_{uuid.uuid4().hex[:8]}"
    legacy_id = f"sch_{uuid.uuid4().hex[:12]}"
    made: list[str] = []
    with admin._engine.begin() as c:  # noqa: SLF001 - テスト用の前提データ投入
        c.execute(
            text("INSERT INTO tenants (id, name) VALUES (:i,:n) ON CONFLICT (id) DO NOTHING"),
            {"i": tenant, "n": "test"},
        )
    try:
        healthy = admin.put_schema(tenant, healthy_type, [SchemaFieldDef(name="total")])
        made.append(healthy.id)
        # 検査導入前の旧データを直接 INSERT で再現する（put_schema は今は拒む）
        with admin._engine.begin() as c:  # noqa: SLF001
            c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant})
            c.execute(
                text(
                    "INSERT INTO field_schemas (id, tenant_id, doc_type, version, fields)"
                    " VALUES (:i, :t, :d, 1, CAST(:f AS jsonb))"
                ),
                {
                    "i": legacy_id, "t": tenant, "d": legacy_type,
                    "f": '[{"name": "__memo", "label": "メモ", "type": "string",'
                    ' "required": false, "critical": false, "columns": null, "region": null}]',
                },
            )
        made.append(legacy_id)

        # 旧データ自身も、同テナントの一覧も読める
        got = admin.get_schema(tenant, legacy_type)
        assert got is not None and [f.name for f in got.fields] == ["__memo"]
        assert admin.get_schema_by_id(tenant, legacy_id) is not None
        listed = {s.doc_type for s in admin.list_schemas(tenant)}
        assert {legacy_type, healthy_type} <= listed

        # 一方、新しく書こうとすると拒む（書き込み側の共通入口）
        import pytest

        with pytest.raises(ValueError, match="予約"):
            admin.put_schema(tenant, healthy_type, [SchemaFieldDef(name="__memo")])
    finally:
        with admin._engine.begin() as c:  # noqa: SLF001
            c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant})
            for sid in made:
                c.execute(text("DELETE FROM field_schemas WHERE id=:i"), {"i": sid})
