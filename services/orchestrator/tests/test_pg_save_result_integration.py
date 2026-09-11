# mypy: ignore-errors
"""PgContextStore.save_result の実 DDL 統合テスト（敵対的レビュー確定所見の固定）。

固定する契約:
1. correction / validation が実際に永続化される（f0bc443 で直した欠落の再発防止。
   InMemory はモデルを丸ごと保持するため、この欠落はローカルでは原理的に検出できない）
2. 再配信の再実行（同 run_id への再保存）は行全体を書き直す（キメラ行防止）
3. 人手確定（corrected/approved）は機械の再抽出（pending/auto）で巻き戻らない
4. confirmed の run/document 状態は needs_review へ巻き戻らない
5. spans（run_spans, 0008）は書ける・confirmed の再保存で消えない・再配信で二重にならない・
   帳票削除で消える（設計 D12 / §2.4）

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

from newfan_schemas import ExtractedField, ReviewStatus, Span  # noqa: E402

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


def test_スキーマレス発見のlabelが実DDLへ永続化される(env) -> None:
    """自動発見（ADR-0006）の見出し原文は extraction_fields.label（0006）が唯一の置き場。

    スキーマ指定の抽出は label を field_schemas から都度引けるが、スキーマレスでは
    LLM 申告の label を行に残さないと、検証画面もテンプレート化ダイアログも
    snake_case の name しか表示できない。InMemory はモデルを丸ごと保持するため
    この欠落はローカルでは検出できない（correction/validation の欠落と同型）。
    """
    from sqlalchemy import text

    owner, store, doc_id, run_id = env
    f = _field("total_amount", "128000", "128000")
    f.label = "御請求金額"
    store.save_result(TENANT, run_id, fields=[f], tables=[], review_items=[],
                      status="needs_review")
    with owner.begin() as c:
        row = c.execute(
            text("SELECT label FROM extraction_fields WHERE run_id=:r AND field_name='total_amount'"),
            {"r": run_id},
        ).first()
    assert row is not None and row[0] == "御請求金額"


def test_gatewayの_syncedがlabelを読み戻す(env) -> None:
    """スキーマレス run の結果 API で label が届くことの end-to-end 固定。

    worker が書いた label を gateway の PgRepository._synced が SELECT に含めないと、
    保存はされているのに API 応答では常に None になる（書けるが読めない片肺）。
    """
    owner, store, doc_id, run_id = env
    f = _field("issuer_name", "株式会社ニューファン", "株式会社ニューファン")
    f.label = "発行者"
    store.save_result(TENANT, run_id, fields=[f], tables=[], review_items=[],
                      status="needs_review")

    from newfan_gateway.db import PgRepository

    repo = PgRepository(_DSN)
    run = repo.get_run(TENANT, run_id)
    assert run is not None
    by_name = {x.name: x for x in run.fields}
    assert by_name["issuer_name"].label == "発行者"


def test_再実行で消えた発見名の旧行を掃除する(env) -> None:
    """スキーマレス自動発見の再配信・再実行で名前集合が揺れても幽霊を残さない。

    UPSERT は「同名の上書き」しかしないため、再実行が別名を発見すると旧行が
    残留し、結果 API とテンプレート化ダイアログに実在しない項目が並ぶ
    （敵対的レビュー確定）。人手確定（corrected/approved）は掃除しない。
    """
    from sqlalchemy import text

    owner, store, doc_id, run_id = env
    # 1回目: total_amount と issuer_name を発見（issuer_name は人手確定にする）
    f1 = _field("total_amount", "128000", "128000")
    f2 = _field("issuer_name", "旧社名", "旧社名", rs=ReviewStatus.CORRECTED)
    store.save_result(TENANT, run_id, fields=[f1, f2], tables=[], review_items=[],
                      status="needs_review")
    # 2回目（再配信の再実行）: LLM が billing_amount と命名し直した
    f3 = _field("billing_amount", "128000", "128000")
    store.save_result(TENANT, run_id, fields=[f3], tables=[], review_items=[],
                      status="needs_review")
    with owner.begin() as c:
        rows = {
            r[0]
            for r in c.execute(
                text("SELECT field_name FROM extraction_fields WHERE run_id=:r"),
                {"r": run_id},
            )
        }
    assert "billing_amount" in rows  # 新発見は入る
    assert "total_amount" not in rows  # 機械由来の旧行は掃除
    assert "issuer_name" in rows  # 人手確定は残す


def test_region_statsがmetricsへマージされる(env) -> None:
    """除外件数が metrics JSONB に載り、fallback_pages を潰さないこと。

    検証画面の除外バッジはこの値だけが根拠（ReviewItem は永続化されない）ので、
    ここが落ちると「N セルを未取込」という所見の裏付けが消える。
    """
    from sqlalchemy import text

    owner, store, _doc, run_id = env
    store.save_result(
        TENANT, run_id,
        fields=[_field("total_amount", "1", "1")], tables=[], review_items=[],
        status="needs_review",
        fallback_pages=[2],
        region_stats={"excluded_spans": 4, "excluded_cells": 2, "excluded_rows": 1},
    )
    with owner.begin() as c:
        m = c.execute(
            text("SELECT metrics FROM extraction_runs WHERE id=:r"), {"r": run_id}
        ).scalar_one()
    # **needs_review 保存の時点で**載っていること（マスク発動 run は必ずここで止まる）
    assert m["region"] == {"excluded_spans": 4, "excluded_cells": 2, "excluded_rows": 1}
    assert m["fallback_pages"] == [2]  # 既存キーを潰さない


def test_region_stats未指定なら既存metricsを壊さない(env) -> None:
    """領域を使っていない run では region キーを作らない（後方互換）。"""
    from sqlalchemy import text

    owner, store, _doc, run_id = env
    store.save_result(
        TENANT, run_id,
        fields=[_field("total_amount", "1", "1")], tables=[], review_items=[],
        status="needs_review", fallback_pages=[],
    )
    with owner.begin() as c:
        m = c.execute(
            text("SELECT metrics FROM extraction_runs WHERE id=:r"), {"r": run_id}
        ).scalar_one()
    assert "region" not in m


def test_フィールドのbboxがNULLでなく保存される(env) -> None:
    """F-0（Phase 0）の永続化を実 DDL で押さえる。

    kie が bbox を作っても保存列へ渡っていなければ検証画面のオーバーレイは出ない。
    """
    from sqlalchemy import text

    owner, store, _doc, run_id = env
    f = _field("total_amount", "128000", "128000")
    f.bbox = [300, 180, 430, 212]
    store.save_result(
        TENANT, run_id, fields=[f], tables=[], review_items=[], status="confirmed"
    )
    with owner.begin() as c:
        row = c.execute(
            text("SELECT page_no, bbox FROM extraction_fields WHERE run_id=:r"), {"r": run_id}
        ).mappings().first()
    assert row["bbox"] == [300, 180, 430, 212]
    assert row["page_no"] == 1


def test_領域を外した再実行で古いregionが消える(env) -> None:
    """同じ run を領域なしで保存し直したら、前回の所見を残さないこと。

    metrics は浅いマージなので、単に「region を書かない」だけだと前回の
    mismatch_fields が残り、検証画面が解消済みの所見を出し続ける
    （参考表示を始めて初めて人の目に見えるようになった穴）。
    """
    from sqlalchemy import text

    owner, store, _doc, run_id = env
    store.save_result(
        TENANT, run_id,
        fields=[_field("total_amount", "1", "1")], tables=[], review_items=[],
        status="needs_review", fallback_pages=[2],
        region_stats={"mismatch_fields": ["issuer_name"], "layout_mismatch": False},
    )
    store.save_result(
        TENANT, run_id,
        fields=[_field("total_amount", "1", "1")], tables=[], review_items=[],
        status="needs_review", fallback_pages=[2],
        region_stats=None,
    )
    with owner.begin() as c:
        m = c.execute(
            text("SELECT metrics FROM extraction_runs WHERE id=:r"), {"r": run_id}
        ).scalar_one()
    assert "region" not in m
    assert m["fallback_pages"] == [2]  # 他のキーは巻き込まない


def test_needs_reviewからconfirmedの再保存でregionが残る(env) -> None:
    """所見つきの run を確定しても region が消えないこと。

    finalize も同じ region を渡すので消えないはず——これが落ちるなら、
    上の「空なら落とす」変更は確定時に所見を消してしまうので入れてはいけない。
    """
    from sqlalchemy import text

    owner, store, _doc, run_id = env
    stats = {"mismatch_fields": ["issuer_name"], "layout_mismatch": False}
    store.save_result(
        TENANT, run_id,
        fields=[_field("total_amount", "1", "1")], tables=[], review_items=[],
        status="needs_review", region_stats=stats,
    )
    store.save_result(
        TENANT, run_id,
        fields=[_field("total_amount", "1", "1")], tables=[], review_items=[],
        status="confirmed", region_stats=stats,
    )
    with owner.begin() as c:
        m = c.execute(
            text("SELECT metrics FROM extraction_runs WHERE id=:r"), {"r": run_id}
        ).scalar_one()
    assert m["region"] == stats


# ---- run_spans（設計 D12 / §2.4） ----


def _span(span_id, page, text_, bbox=(10, 20, 110, 40), conf=0.95):
    return Span(
        span_id=span_id, page=page, text=text_, conf=conf, bbox=list(bbox),
        # 保存対象外の付随情報。これが JSONB に漏れないことも見る
        char_boxes=[[10, 20, 30, 40]], char_confs=[0.9], block_id=7,
    )


def _span_rows(owner, run_id):
    """run_spans を page_no → spans で返す。"""
    from sqlalchemy import text

    with owner.begin() as c:
        return {
            r[0]: r[1]
            for r in c.execute(
                text("SELECT page_no, spans FROM run_spans WHERE run_id=:r ORDER BY page_no"),
                {"r": run_id},
            )
        }


def test_spansがrun_spansへページごとに保存される(env) -> None:
    """(a) 渡した span が run × ページで JSONB に入り、1 span は 4 キーだけになること。

    page は行のキーなので span 側には書かない。char_boxes 等の付随情報も書かない
    （1 ページ数百 span × 100 バイト程度に収める。設計 §2.4）。
    """
    owner, store, _doc, run_id = env
    store.save_result(
        TENANT, run_id,
        fields=[_field("total_amount", "1", "1")], tables=[], review_items=[],
        status="needs_review",
        spans=[
            _span(1, 1, "株式会社千曲川ホーム"),
            _span(2, 1, "御請求書", bbox=(300, 20, 400, 40), conf=0.8),
            _span(3, 2, "2 ページ目", bbox=(5, 5, 50, 15)),
        ],
    )
    rows = _span_rows(owner, run_id)
    assert set(rows) == {1, 2}
    assert rows[1] == [
        {"span_id": 1, "text": "株式会社千曲川ホーム", "bbox": [10, 20, 110, 40], "conf": 0.95},
        {"span_id": 2, "text": "御請求書", "bbox": [300, 20, 400, 40], "conf": 0.8},
    ]
    assert rows[2] == [{"span_id": 3, "text": "2 ページ目", "bbox": [5, 5, 50, 15], "conf": 0.95}]
    # tenant_id が行に載っている（RLS の判定列）
    from sqlalchemy import text

    with owner.begin() as c:
        tenants = {
            r[0] for r in c.execute(
                text("SELECT tenant_id FROM run_spans WHERE run_id=:r"), {"r": run_id}
            )
        }
    assert tenants == {TENANT}


def test_spans無しのconfirmed再保存でrun_spansが残る(env) -> None:
    """(b) needs_review 保存で書いた span が、finalize（spans=None）で消えないこと。

    metrics.region で踏んだ「needs_review → confirmed で消える」穴と同型。resume 経路の
    finalize は state に spans を持たないことがあるため、None／空は「触らない」に倒す。
    """
    owner, store, _doc, run_id = env
    store.save_result(
        TENANT, run_id,
        fields=[_field("total_amount", "1", "1")], tables=[], review_items=[],
        status="needs_review", spans=[_span(1, 1, "残るべき文字")],
    )
    store.save_result(
        TENANT, run_id,
        fields=[_field("total_amount", "1", "1", rs=ReviewStatus.APPROVED)], tables=[],
        review_items=[], status="confirmed", spans=None,
    )
    # 空リストも「触らない」
    store.save_result(
        TENANT, run_id,
        fields=[_field("total_amount", "1", "1", rs=ReviewStatus.APPROVED)], tables=[],
        review_items=[], status="confirmed", spans=[],
    )
    rows = _span_rows(owner, run_id)
    assert rows == {1: [{"span_id": 1, "text": "残るべき文字", "bbox": [10, 20, 110, 40], "conf": 0.95}]}


def test_同じspansの再保存でページごとに1行のまま(env) -> None:
    """(c) 再配信の再実行（同 run_id への再保存）で行が増えず、中身は新しい方になること。"""
    owner, store, _doc, run_id = env
    store.save_result(
        TENANT, run_id,
        fields=[_field("total_amount", "1", "1")], tables=[], review_items=[],
        status="needs_review", spans=[_span(1, 1, "1 回目"), _span(2, 2, "1 回目 p2")],
    )
    # OCR 揺れで原文が変わった再実行
    store.save_result(
        TENANT, run_id,
        fields=[_field("total_amount", "1", "1")], tables=[], review_items=[],
        status="needs_review", spans=[_span(1, 1, "2 回目"), _span(2, 2, "2 回目 p2")],
    )
    from sqlalchemy import text

    with owner.begin() as c:
        n = c.execute(
            text("SELECT count(*) FROM run_spans WHERE run_id=:r"), {"r": run_id}
        ).scalar_one()
    assert n == 2  # page 1 と 2 で 1 行ずつ
    rows = _span_rows(owner, run_id)
    assert rows[1][0]["text"] == "2 回目"
    assert rows[2][0]["text"] == "2 回目 p2"


def test_帳票削除でrun_spansが消える(env) -> None:
    """(d) documents → extraction_runs → run_spans の CASCADE。delete_document の手順は変えない。"""
    from sqlalchemy import text

    owner, store, doc_id, run_id = env
    store.save_result(
        TENANT, run_id,
        fields=[_field("total_amount", "1", "1")], tables=[], review_items=[],
        status="needs_review", spans=[_span(1, 1, "消えるべき文字")],
    )
    assert _span_rows(owner, run_id)
    with owner.begin() as c:
        c.execute(text("DELETE FROM documents WHERE id=:d"), {"d": doc_id})
    assert _span_rows(owner, run_id) == {}


def test_dict形のspansでも保存できる(env) -> None:
    """checkpoint 復元経路で Span モデルでなく素の dict が来ても同じ形で書けること。"""
    owner, store, _doc, run_id = env
    store.save_result(
        TENANT, run_id,
        fields=[_field("total_amount", "1", "1")], tables=[], review_items=[],
        status="needs_review",
        spans=[{"span_id": 9, "page": 1, "text": "dict 由来", "conf": 0.5, "bbox": [1, 2, 3, 4]}],
    )
    assert _span_rows(owner, run_id) == {
        1: [{"span_id": 9, "text": "dict 由来", "bbox": [1, 2, 3, 4], "conf": 0.5}]
    }
