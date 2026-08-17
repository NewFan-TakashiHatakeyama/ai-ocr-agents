"""gdrive 接続（⑤⑥ SaaS連携）: 作成と「今すぐ同期」。"""

from types import SimpleNamespace

from gw_helpers import auth


def _create(ctx: SimpleNamespace, config=None):
    return ctx.client.post(
        "/v1/connections",
        headers=auth("admin"),
        json={
            "type": "gdrive",
            "name": "経理共有ドライブ",
            "config": config if config is not None else {"folder_id": "inbox-folder"},
        },
    )


def test_gdrive接続を作成できる(ctx: SimpleNamespace) -> None:
    r = _create(ctx)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["type"] == "gdrive"
    assert body["config"]["folder_id"] == "inbox-folder"


def test_gdrive接続はfolder_id必須(ctx: SimpleNamespace) -> None:
    # folder_id が無いと何も検知できないサイレント故障になるため作成時に断る
    r = _create(ctx, config={})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "E4001"


def test_今すぐ同期はq_syncへ積まれる(ctx: SimpleNamespace) -> None:
    cid = _create(ctx).json()["id"]
    r = ctx.client.post(f"/v1/connections/{cid}/sync", headers=auth("admin"))
    assert r.status_code == 202, r.text
    assert r.json()["queued"] is True
    # worker 側ポーラーが消費する q.sync に tenant/connection が積まれる
    msgs = [m for s, m in ctx.queue.messages if s == "q.sync"]
    assert msgs and msgs[-1]["connection_id"] == cid
    assert msgs[-1]["tenant_id"] == "ten_1"


def test_今すぐ同期はgdrive以外を断る(ctx: SimpleNamespace) -> None:
    r = ctx.client.post(
        "/v1/connections",
        headers=auth("admin"),
        json={"type": "webhook", "name": "wh", "config": {"url": "https://example.com/hook"}},
    )
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    sync = ctx.client.post(f"/v1/connections/{cid}/sync", headers=auth("admin"))
    assert sync.status_code == 409
    assert sync.json()["error"]["code"] == "E1005"


def test_今すぐ同期はadmin限定(ctx: SimpleNamespace) -> None:
    cid = _create(ctx).json()["id"]
    r = ctx.client.post(f"/v1/connections/{cid}/sync", headers=auth("reviewer"))
    assert r.status_code == 403


# ---- 敵対的レビュー確定所見の回帰（2026-08-17） ----


def test_今すぐ同期の連打はデバウンスされる(ctx: SimpleNamespace) -> None:
    # 連打で q.sync が無制限に滞留すると共有 worker 主ループと Drive quota を占有できる
    cid = _create(ctx).json()["id"]
    r1 = ctx.client.post(f"/v1/connections/{cid}/sync", headers=auth("admin"))
    r2 = ctx.client.post(f"/v1/connections/{cid}/sync", headers=auth("admin"))
    assert r1.json()["queued"] is True
    assert r2.json()["queued"] is False  # 30秒以内の再要求は enqueue しない
    msgs = [m for s, m in ctx.queue.messages if s == "q.sync" and m["connection_id"] == cid]
    assert len(msgs) == 1


def test_無効化された接続は同期できない(ctx: SimpleNamespace) -> None:
    cid = _create(ctx).json()["id"]
    ctx.admin.set_connection_status("ten_1", cid, "disabled")
    r = ctx.client.post(f"/v1/connections/{cid}/sync", headers=auth("admin"))
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "E1005"


def test_folder_idのクォートや改行は拒否される(ctx: SimpleNamespace) -> None:
    # Drive クエリ（'<id>' in parents）を破壊しサイレント故障化するため作成時に断る
    for bad in ("abc'def", "abc\ndef", "x" * 201):
        r = _create(ctx, config={"folder_id": bad})
        assert r.status_code == 422, bad
        assert r.json()["error"]["code"] == "E4001"


def test_folder_idは正規化して保存される(ctx: SimpleNamespace) -> None:
    r = _create(ctx, config={"folder_id": "  keiri-inbox  "})
    assert r.status_code == 201, r.text
    assert r.json()["config"]["folder_id"] == "keiri-inbox"
