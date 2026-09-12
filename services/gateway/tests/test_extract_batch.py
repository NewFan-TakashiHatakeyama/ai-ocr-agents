"""POST /documents/extract-batch（設計 bulk-processing §2）。

帳票ごとに単体 /extract と同じ判定を通し、通らなかった帳票は skipped に理由付きで
載せて続行する。ここで守るのは「1 件の事情で一括全体が止まらない」「確定済みは
黙って置き換えない」「他テナント・不在は E1001 で skipped（404 にしない）」。
"""

from __future__ import annotations

from types import SimpleNamespace

from gw_helpers import PDF, auth, make_token

from newfan_gateway.records import DocumentRecord, RunRecord, SchemaFieldDef, SchemaRecord


def _upload(ctx: SimpleNamespace, doc_type: str | None = "invoice", tenant: str = "ten_1") -> str:
    headers = {"Authorization": f"Bearer {make_token(role='uploader', tenant=tenant)}"}
    r = ctx.client.post(
        "/v1/documents",
        headers=headers,
        files={"file": ("x.pdf", PDF, "application/pdf")},
        data={"doc_type": doc_type} if doc_type else {},
    )
    assert r.status_code == 201, r.text
    return r.json()["document_id"]


def _seed_run(ctx: SimpleNamespace, doc_id: str, status: str, run_id: str | None = None) -> str:
    run = RunRecord(
        id=run_id or f"run_{status}_{doc_id[-6:]}",
        tenant_id="ten_1",
        document_id=doc_id,
        status=status,
    )
    ctx.repo.create_run(run)
    return run.id


def _batch(ctx: SimpleNamespace, body: dict, headers: dict | None = None):
    return ctx.client.post(
        "/v1/documents/extract-batch", headers=headers or auth("uploader"), json=body
    )


def test_batch_by_ids_accepts_and_skips_with_reasons(ctx: SimpleNamespace) -> None:
    """accepted / skipped の振り分け: 確定済み（E1005）・種別なし・スキーマなし・
    他テナント・不在（E1001）。1 件の事情で全体は止まらない。"""
    ok = _upload(ctx, "invoice")
    confirmed = _upload(ctx, "invoice")
    _seed_run(ctx, confirmed, "confirmed")
    no_type = _upload(ctx, None)
    no_schema = _upload(ctx, "receipt")  # conftest に receipt のスキーマは無い
    foreign = _upload(ctx, "invoice", tenant="ten_2")

    r = _batch(
        ctx,
        {
            "document_ids": [ok, confirmed, no_type, no_schema, foreign, "doc_missing"],
            "supersede_review": True,
        },
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["truncated"] is False
    assert [a["document_id"] for a in body["accepted"]] == [ok]
    acc = body["accepted"][0]
    assert acc["job_id"].startswith("job_") and acc["run_id"].startswith("run_")
    # 種別 invoice の最新版（conftest の sch_1）で走る
    assert ctx.repo.get_run("ten_1", acc["run_id"]).schema_id == "sch_1"
    assert ctx.repo.get_document("ten_1", ok).status == "queued"

    by_id = {s["document_id"]: s for s in body["skipped"]}
    assert set(by_id) == {confirmed, no_type, no_schema, foreign, "doc_missing"}
    assert by_id[confirmed]["code"] == "E1005"
    assert "確定" in by_id[confirmed]["message"]
    assert by_id[no_type]["code"] == "no_schema"
    assert by_id[no_schema]["code"] == "no_schema"
    assert "receipt" in by_id[no_schema]["message"]
    assert by_id[foreign]["code"] == "E1001"
    assert by_id["doc_missing"]["code"] == "E1001"
    # 確定済み・他テナントは触っていない
    assert ctx.repo.get_latest_run("ten_1", confirmed).status == "confirmed"
    assert ctx.repo.get_document("ten_2", foreign).status == "uploaded"
    # キューには受理した分だけ
    assert len(ctx.queue.messages) == 1


def test_batch_needs_review_requires_supersede(ctx: SimpleNamespace) -> None:
    """既定（supersede_review=false）では needs_review は競合として skipped、
    true なら旧 run を superseded に落として受理する（単体 /extract と同じ意味論）。"""
    doc = _upload(ctx, "invoice")
    old = _seed_run(ctx, doc, "needs_review")

    r = _batch(ctx, {"document_ids": [doc]})
    assert r.status_code == 202
    assert r.json()["accepted"] == []
    assert r.json()["skipped"][0]["code"] == "E1005"
    assert ctx.repo.get_run("ten_1", old).status == "needs_review"

    r2 = _batch(ctx, {"document_ids": [doc], "supersede_review": True})
    assert [a["document_id"] for a in r2.json()["accepted"]] == [doc]
    assert ctx.repo.get_run("ten_1", old).status == "superseded"


def test_batch_explicit_schema_id_applies_to_all(ctx: SimpleNamespace) -> None:
    """schema_id を明示すると種別に関係なくその版で走る（種別なしの帳票も対象になる）。
    存在しない schema_id は 1 件も触らずに E1001。"""
    a = _upload(ctx, None)
    b = _upload(ctx, "receipt")
    r = _batch(ctx, {"document_ids": [a, b], "schema_id": "sch_1"})
    assert r.status_code == 202
    assert {x["document_id"] for x in r.json()["accepted"]} == {a, b}
    for x in r.json()["accepted"]:
        assert ctx.repo.get_run("ten_1", x["run_id"]).schema_id == "sch_1"

    c = _upload(ctx, "invoice")
    bad = _batch(ctx, {"document_ids": [c], "schema_id": "sch_nope"})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "E1001"
    assert ctx.repo.get_document("ten_1", c).status == "uploaded"


def test_batch_validates_target_shape(ctx: SimpleNamespace) -> None:
    """document_ids と doc_type はどちらか一方。statuses は doc_type とだけ。"""
    doc = _upload(ctx, "invoice")
    assert _batch(ctx, {}).status_code == 422
    assert _batch(ctx, {}).json()["error"]["code"] == "E1003"
    assert _batch(ctx, {"document_ids": [doc], "doc_type": "invoice"}).status_code == 422
    assert _batch(ctx, {"document_ids": [doc], "statuses": ["uploaded"]}).status_code == 422
    assert _batch(ctx, {"doc_type": "invoice", "statuses": []}).status_code == 422
    # 何も触っていない
    assert ctx.repo.get_document("ten_1", doc).status == "uploaded"


def test_batch_caps_ids_at_200(ctx: SimpleNamespace) -> None:
    ids = [f"doc_{i:04d}" for i in range(201)]
    r = _batch(ctx, {"document_ids": ids})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "E1003" and err["details"] == {"count": 201, "max": 200}
    # 重複は 1 回に数える（200 件ぶんの重複入りは上限に掛からない）
    doc = _upload(ctx, "invoice")
    r2 = _batch(ctx, {"document_ids": [doc] * 300})
    assert r2.status_code == 202
    assert [a["document_id"] for a in r2.json()["accepted"]] == [doc]
    assert r2.json()["skipped"] == []


def test_batch_idempotency_replay(ctx: SimpleNamespace) -> None:
    """同じ Idempotency-Key の再送は同じ応答を返し、run を増やさない。"""
    doc = _upload(ctx, "invoice")
    headers = {**auth("uploader"), "Idempotency-Key": "bulk-1"}
    r1 = _batch(ctx, {"document_ids": [doc]}, headers)
    r2 = _batch(ctx, {"document_ids": [doc]}, headers)
    assert r1.status_code == 202 and r2.status_code == 202
    assert r1.json() == r2.json()
    assert len(r1.json()["accepted"]) == 1
    assert len(ctx.queue.messages) == 1
    # 別キーなら通常どおり判定される（今度は処理中なので競合）
    r3 = _batch(ctx, {"document_ids": [doc]}, {**auth("uploader"), "Idempotency-Key": "bulk-2"})
    assert r3.json()["skipped"][0]["code"] == "E1005"
    # 単体 /extract と同じキーを使い回しても形が混ざらない
    single = ctx.client.post(
        f"/v1/documents/{_upload(ctx, 'invoice')}/extract",
        headers={**auth("uploader"), "Idempotency-Key": "bulk-1"},
        json={},
    )
    assert single.status_code == 202 and set(single.json()) == {"job_id", "run_id"}


def test_batch_by_doc_type_uses_default_statuses(ctx: SimpleNamespace) -> None:
    """doc_type 指定の既定の母集合は uploaded / needs_review / failed。
    確定済みと他種別は母集合に入らない（skipped にも出ない）。"""
    up = _upload(ctx, "invoice")
    failed = _upload(ctx, "invoice")
    ctx.repo.set_document_status("ten_1", failed, "failed")
    review = _upload(ctx, "invoice")
    ctx.repo.set_document_status("ten_1", review, "needs_review")
    old = _seed_run(ctx, review, "needs_review")
    confirmed = _upload(ctx, "invoice")
    ctx.repo.set_document_status("ten_1", confirmed, "confirmed")
    _seed_run(ctx, confirmed, "confirmed")
    _upload(ctx, "receipt")

    r = _batch(ctx, {"doc_type": "invoice", "supersede_review": True})
    assert r.status_code == 202, r.text
    body = r.json()
    assert {a["document_id"] for a in body["accepted"]} == {up, failed, review}
    assert body["skipped"] == []
    assert body["truncated"] is False
    assert ctx.repo.get_run("ten_1", old).status == "superseded"
    assert ctx.repo.get_latest_run("ten_1", confirmed).status == "confirmed"

    # statuses を明示すればその範囲だけ
    fresh = _upload(ctx, "invoice")
    r2 = _batch(ctx, {"doc_type": "invoice", "statuses": ["uploaded"]})
    assert [a["document_id"] for a in r2.json()["accepted"]] == [fresh]


def test_batch_by_doc_type_without_schema_skips_all(ctx: SimpleNamespace) -> None:
    """種別はあるがスキーマが無い（削除・未作成）なら全件 no_schema。"""
    a = _upload(ctx, "receipt")
    r = _batch(ctx, {"doc_type": "receipt"})
    assert r.status_code == 202
    assert r.json()["accepted"] == []
    assert [(s["document_id"], s["code"]) for s in r.json()["skipped"]] == [(a, "no_schema")]


def test_batch_by_doc_type_truncates_to_newest_200(ctx: SimpleNamespace) -> None:
    """母集合が上限を超えたら新しい順に 200 件で切り、truncated=true で知らせる。"""
    from datetime import datetime, timedelta, timezone

    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ids = []
    for i in range(201):
        did = f"doc_bulk_{i:03d}"
        ctx.repo.create_document(
            DocumentRecord(
                id=did, tenant_id="ten_1", storage_uri="s3://b/k", mime_type="image/png",
                doc_type="invoice", status="uploaded", created_at=base + timedelta(minutes=i),
            ),
            [],
        )
        ids.append(did)
    r = _batch(ctx, {"doc_type": "invoice"})
    assert r.status_code == 202
    body = r.json()
    assert body["truncated"] is True
    accepted = [a["document_id"] for a in body["accepted"]]
    assert len(accepted) == 200
    assert "doc_bulk_000" not in accepted  # 最も古い 1 件が外れる
    assert accepted[0] == "doc_bulk_200"  # 新しい順
    assert len(ctx.queue.messages) == 200


def test_batch_uses_latest_schema_version_per_doc_type(ctx: SimpleNamespace) -> None:
    """schema_id 省略時は帳票の種別の**最新版**（get_schema）を使う。"""
    ctx.admin.seed_schema(
        SchemaRecord(
            id="sch_1_v5", tenant_id="ten_1", doc_type="invoice", version=5,
            fields=[SchemaFieldDef(name="total_amount", type="money_jpy")],
        )
    )
    doc = _upload(ctx, "invoice")
    r = _batch(ctx, {"document_ids": [doc]})
    run_id = r.json()["accepted"][0]["run_id"]
    assert ctx.repo.get_run("ten_1", run_id).schema_id == "sch_1_v5"


def test_batch_requires_uploader(ctx: SimpleNamespace) -> None:
    doc = _upload(ctx, "invoice")
    r = _batch(ctx, {"document_ids": [doc]}, auth("viewer"))
    assert r.status_code == 403


def test_single_extract_behaviour_unchanged_after_refactor(ctx: SimpleNamespace) -> None:
    """単体 /extract は共通化の後も同じ順序で拒否する（不在 → 冪等 → スキーマ → 競合）。"""
    doc = _upload(ctx, "invoice")
    missing = ctx.client.post(
        "/v1/documents/doc_nope/extract",
        headers={**auth("uploader"), "Idempotency-Key": "k"},
        json={},
    )
    assert missing.status_code == 400 and missing.json()["error"]["code"] == "E1001"
    bad_schema = ctx.client.post(
        f"/v1/documents/{doc}/extract", headers=auth("uploader"), json={"schema_id": "sch_x"}
    )
    assert bad_schema.status_code == 400
    ok = ctx.client.post(f"/v1/documents/{doc}/extract", headers=auth("uploader"), json={})
    assert ok.status_code == 202
    again = ctx.client.post(f"/v1/documents/{doc}/extract", headers=auth("uploader"), json={})
    assert again.status_code == 409 and again.json()["error"]["code"] == "E1005"
