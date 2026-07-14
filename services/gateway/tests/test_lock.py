from types import SimpleNamespace

from gw_helpers import PDF, make_token

from newfan_gateway.records import RunRecord


def _rv(sub: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token(role='reviewer', sub=sub)}"}


def _upload(ctx: SimpleNamespace) -> str:
    r = ctx.client.post(
        "/v1/documents",
        headers={"Authorization": f"Bearer {make_token(role='uploader')}"},
        files={"file": ("invoice.pdf", PDF, "application/pdf")},
    )
    return r.json()["document_id"]


def test_acquire_grants_and_reports_holder(ctx: SimpleNamespace) -> None:
    doc_id = _upload(ctx)
    r = ctx.client.post(f"/v1/documents/{doc_id}/lock", headers=_rv("alice"))
    assert r.status_code == 200
    b = r.json()
    assert b["locked"] is True
    assert b["held_by_me"] is True
    assert b["holder"] == "alice"
    assert 0 < b["remaining_sec"] <= 300


def test_second_reviewer_is_blocked(ctx: SimpleNamespace) -> None:
    doc_id = _upload(ctx)
    ctx.client.post(f"/v1/documents/{doc_id}/lock", headers=_rv("alice"))
    r = ctx.client.post(f"/v1/documents/{doc_id}/lock", headers=_rv("bob"))
    assert r.status_code == 200  # 助言的ロックなのでエラーにしない
    b = r.json()
    assert b["locked"] is True
    assert b["held_by_me"] is False  # bob は読取専用
    assert b["holder"] == "alice"


def test_get_lock_reflects_holder(ctx: SimpleNamespace) -> None:
    doc_id = _upload(ctx)
    ctx.client.post(f"/v1/documents/{doc_id}/lock", headers=_rv("alice"))
    # bob からポーリング
    r = ctx.client.get(f"/v1/documents/{doc_id}/lock", headers=_rv("bob"))
    assert r.json() == {
        "document_id": doc_id,
        "locked": True,
        "held_by_me": False,
        "holder": "alice",
        "remaining_sec": r.json()["remaining_sec"],
        "ttl_sec": 300,
    }
    # alice 自身からは held_by_me
    r2 = ctx.client.get(f"/v1/documents/{doc_id}/lock", headers=_rv("alice"))
    assert r2.json()["held_by_me"] is True


def test_release_frees_lock(ctx: SimpleNamespace) -> None:
    doc_id = _upload(ctx)
    ctx.client.post(f"/v1/documents/{doc_id}/lock", headers=_rv("alice"))
    rel = ctx.client.delete(f"/v1/documents/{doc_id}/lock", headers=_rv("alice"))
    assert rel.status_code == 200
    assert rel.json()["locked"] is False
    # bob が取得できる
    r = ctx.client.post(f"/v1/documents/{doc_id}/lock", headers=_rv("bob"))
    assert r.json()["held_by_me"] is True


def test_non_holder_cannot_release(ctx: SimpleNamespace) -> None:
    doc_id = _upload(ctx)
    ctx.client.post(f"/v1/documents/{doc_id}/lock", headers=_rv("alice"))
    # bob の解放は無効（alice のロックは残る）
    ctx.client.delete(f"/v1/documents/{doc_id}/lock", headers=_rv("bob"))
    r = ctx.client.get(f"/v1/documents/{doc_id}/lock", headers=_rv("bob"))
    assert r.json()["locked"] is True
    assert r.json()["holder"] == "alice"


def test_confirm_releases_lock(ctx: SimpleNamespace) -> None:
    doc_id = _upload(ctx)
    ctx.repo.create_run(
        RunRecord(
            id="run_lock1",
            tenant_id="ten_1",
            document_id=doc_id,
            status="needs_review",
            result_version=1,
            review_summary={"pending": 0, "auto": 1},
        )
    )
    ctx.client.post(f"/v1/documents/{doc_id}/lock", headers=_rv("alice"))
    ok = ctx.client.post(
        f"/v1/documents/{doc_id}/confirm", headers=_rv("alice"), json={"run_id": "run_lock1"}
    )
    assert ok.status_code == 202
    # 確定でロックが解放され、bob が取得できる
    r = ctx.client.post(f"/v1/documents/{doc_id}/lock", headers=_rv("bob"))
    assert r.json()["held_by_me"] is True


def test_lock_requires_reviewer(ctx: SimpleNamespace) -> None:
    doc_id = _upload(ctx)
    r = ctx.client.post(
        f"/v1/documents/{doc_id}/lock",
        headers={"Authorization": f"Bearer {make_token(role='uploader')}"},
    )
    assert r.status_code == 403


def test_store_expired_lock_is_stealable() -> None:
    from newfan_gateway.locks import InMemoryLockStore

    store = InMemoryLockStore()
    acquired, _ = store.acquire("ten_1", "doc_1", "alice", ttl_sec=0)  # 即時失効
    assert acquired is True
    assert store.get("ten_1", "doc_1") is None  # 期限切れは None
    # 失効済みなので bob が奪取できる
    acquired2, info = store.acquire("ten_1", "doc_1", "bob", ttl_sec=0)
    assert acquired2 is True
    assert info.holder_sub == "bob"


def test_store_refresh_by_same_holder() -> None:
    from newfan_gateway.locks import InMemoryLockStore

    store = InMemoryLockStore()
    store.acquire("ten_1", "doc_1", "alice")
    acquired, info = store.acquire("ten_1", "doc_1", "alice")  # 更新
    assert acquired is True
    assert info.holder_sub == "alice"
