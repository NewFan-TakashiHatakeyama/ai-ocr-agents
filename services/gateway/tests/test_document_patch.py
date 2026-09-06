"""PATCH /documents/{id}（帳票種別の書き戻し）。

テンプレート化したあと、その帳票自身の doc_type を確定するための API。
これが無いと、次に同じ種別が来ても classify の declared 経路
（routers.py の `doc.doc_type in latest` 完全一致）に載らない。
"""

from types import SimpleNamespace

from gw_helpers import PDF, auth, make_token


def _upload(ctx: SimpleNamespace, filename: str = "scan.pdf", doc_type: str | None = None) -> str:
    r = ctx.client.post(
        "/v1/documents",
        headers=auth("uploader"),
        files={"file": (filename, PDF, "application/pdf")},
        data={"doc_type": doc_type} if doc_type else {},
    )
    assert r.status_code == 201, r.text
    return str(r.json()["document_id"])


def test_patch_doc_typeで帳票種別を確定できる(ctx: SimpleNamespace) -> None:
    doc_id = _upload(ctx)
    r = ctx.client.patch(
        f"/v1/documents/{doc_id}", headers=auth("admin"), json={"doc_type": "invoice"}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["doc_type"] == "invoice"
    # 単体取得の DocumentMeta 規約（pages を埋める）を PATCH の応答でも守る。
    # ここが空だと web が PATCH 応答をキャッシュへ入れた瞬間に領域編集が壊れる。
    assert body["pages"], body
    got = ctx.client.get(f"/v1/documents/{doc_id}", headers=auth("viewer")).json()
    assert got["doc_type"] == "invoice"


def test_patch_doc_type_未登録の種別は拒否する(ctx: SimpleNamespace) -> None:
    # 任意文字列を通すと「書き戻したのに declared にならない」が無言で成立する
    doc_id = _upload(ctx)
    r = ctx.client.patch(
        f"/v1/documents/{doc_id}", headers=auth("admin"), json={"doc_type": "請求書"}
    )
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "E1001"
    got = ctx.client.get(f"/v1/documents/{doc_id}", headers=auth("viewer")).json()
    assert got["doc_type"] is None


def test_patch_doc_type_空文字は拒否する(ctx: SimpleNamespace) -> None:
    doc_id = _upload(ctx)
    r = ctx.client.patch(
        f"/v1/documents/{doc_id}", headers=auth("admin"), json={"doc_type": "   "}
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "E1003"


def test_patch_doc_type_はadmin限定(ctx: SimpleNamespace) -> None:
    doc_id = _upload(ctx)
    for role in ("uploader", "viewer", "reviewer"):
        r = ctx.client.patch(
            f"/v1/documents/{doc_id}", headers=auth(role), json={"doc_type": "invoice"}
        )
        assert r.status_code == 403, (role, r.text)


def test_patch_doc_type_存在しない帳票(ctx: SimpleNamespace) -> None:
    r = ctx.client.patch(
        "/v1/documents/doc_nope", headers=auth("admin"), json={"doc_type": "invoice"}
    )
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "E1001"


def test_patch_doc_type_他テナントからは触れない(ctx: SimpleNamespace) -> None:
    doc_id = _upload(ctx)
    other = {"Authorization": f"Bearer {make_token(role='admin', tenant='ten_2')}"}
    r = ctx.client.patch(f"/v1/documents/{doc_id}", headers=other, json={"doc_type": "invoice"})
    assert r.status_code == 400, r.text
    got = ctx.client.get(f"/v1/documents/{doc_id}", headers=auth("viewer")).json()
    assert got["doc_type"] is None


def test_patch_doc_type_は削除ブロックの窓を進めない(ctx: SimpleNamespace) -> None:
    """種別を直しただけで削除が stale_minutes 分ブロックされてはいけない。

    set_document_status は updated_at を進めるが、それは status 遷移＝処理が
    動いた証拠だから。メタ情報の更新でそこを進めると副作用になる。
    """
    doc_id = _upload(ctx)
    ctx.repo.set_document_status("ten_1", doc_id, "in_review")
    before = ctx.repo.get_delete_blocker("ten_1", doc_id, stale_minutes=0)
    r = ctx.client.patch(
        f"/v1/documents/{doc_id}", headers=auth("admin"), json={"doc_type": "invoice"}
    )
    assert r.status_code == 200, r.text
    assert ctx.repo.get_delete_blocker("ten_1", doc_id, stale_minutes=0) == before


def test_patch_doc_type_は冪等(ctx: SimpleNamespace) -> None:
    doc_id = _upload(ctx, doc_type="invoice")
    for _ in range(2):
        r = ctx.client.patch(
            f"/v1/documents/{doc_id}", headers=auth("admin"), json={"doc_type": "invoice"}
        )
        assert r.status_code == 200, r.text
        assert r.json()["doc_type"] == "invoice"


def test_patch_後にclassifyがdeclaredになる(ctx: SimpleNamespace) -> None:
    """このエンドポイントの存在理由そのもの。

    ファイル名に手がかりが無い帳票は classify が何も提案できないが、
    書き戻すと declared（confidence 1.0）で最新スキーマを提案するようになる。
    """
    doc_id = _upload(ctx, filename="0001.pdf")
    before = ctx.client.post(f"/v1/documents/{doc_id}/classify", headers=auth("viewer")).json()
    assert before["method"] != "declared"
    assert before["suggested_schema_id"] is None

    ctx.client.patch(
        f"/v1/documents/{doc_id}", headers=auth("admin"), json={"doc_type": "invoice"}
    )
    after = ctx.client.post(f"/v1/documents/{doc_id}/classify", headers=auth("viewer")).json()
    assert after["method"] == "declared"
    assert after["confidence"] == 1.0
    assert after["suggested_schema_id"] == "sch_1"
