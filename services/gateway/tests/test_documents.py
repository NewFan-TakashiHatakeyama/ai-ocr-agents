from types import SimpleNamespace

from gw_helpers import PDF, auth


def _upload(ctx: SimpleNamespace) -> str:
    r = ctx.client.post(
        "/v1/documents",
        headers=auth("uploader"),
        files={"file": ("invoice.pdf", PDF, "application/pdf")},
        data={"doc_type": "invoice", "external_ref": "ERP-1"},
    )
    assert r.status_code == 201, r.text
    return r.json()["document_id"]


def test_upload_creates_document(ctx: SimpleNamespace) -> None:
    doc_id = _upload(ctx)
    assert doc_id.startswith("doc_")
    r = ctx.client.get(f"/v1/documents/{doc_id}", headers=auth("viewer"))
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "uploaded"
    assert body["doc_type"] == "invoice"
    assert body["page_count"] == 1


def test_upload_rejects_bad_format(ctx: SimpleNamespace) -> None:
    r = ctx.client.post(
        "/v1/documents",
        headers=auth("uploader"),
        files={"file": ("x.pdf", b"not-a-pdf", "application/pdf")},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "E1001"


def test_list_documents(ctx: SimpleNamespace) -> None:
    _upload(ctx)
    _upload(ctx)
    r = ctx.client.get("/v1/documents", headers=auth("viewer"))
    assert r.status_code == 200
    assert len(r.json()["items"]) == 2


def _upload_as(ctx: SimpleNamespace, doc_type: str | None, status: str) -> str:
    data = {"doc_type": doc_type} if doc_type else {}
    r = ctx.client.post(
        "/v1/documents",
        headers=auth("uploader"),
        files={"file": ("x.pdf", PDF, "application/pdf")},
        data=data,
    )
    assert r.status_code == 201, r.text
    doc_id = r.json()["document_id"]
    if status != "uploaded":
        ctx.repo.set_document_status("ten_1", doc_id, status)
    return doc_id


def test_list_documents_filters_by_doc_type(ctx: SimpleNamespace) -> None:
    """doc_type は完全一致。種別なし（null）の帳票は含まない（一括再抽出の母集合）。"""
    inv = _upload_as(ctx, "invoice", "uploaded")
    _upload_as(ctx, "receipt", "uploaded")
    _upload_as(ctx, None, "uploaded")
    r = ctx.client.get("/v1/documents?doc_type=invoice", headers=auth("viewer"))
    assert r.status_code == 200
    assert [d["document_id"] for d in r.json()["items"]] == [inv]


def test_list_documents_filters_by_repeated_status(ctx: SimpleNamespace) -> None:
    """status は繰り返し指定で OR。従来の 1 値指定も同じ経路で通る。"""
    up = _upload_as(ctx, "invoice", "uploaded")
    failed = _upload_as(ctx, "invoice", "failed")
    _upload_as(ctx, "invoice", "confirmed")

    r = ctx.client.get(
        "/v1/documents?status=uploaded&status=failed", headers=auth("viewer")
    )
    assert r.status_code == 200
    assert {d["document_id"] for d in r.json()["items"]} == {up, failed}

    r1 = ctx.client.get("/v1/documents?status=failed", headers=auth("viewer"))
    assert [d["document_id"] for d in r1.json()["items"]] == [failed]


def test_list_documents_combines_doc_type_and_status(ctx: SimpleNamespace) -> None:
    """doc_type と status は AND。他テナントの帳票は混ざらない。"""
    from gw_helpers import make_token

    hit = _upload_as(ctx, "invoice", "failed")
    _upload_as(ctx, "invoice", "confirmed")
    _upload_as(ctx, "receipt", "failed")
    other = {"Authorization": f"Bearer {make_token(role='uploader', tenant='ten_2')}"}
    ctx.client.post(
        "/v1/documents",
        headers=other,
        files={"file": ("x.pdf", PDF, "application/pdf")},
        data={"doc_type": "invoice"},
    )
    r = ctx.client.get(
        "/v1/documents?doc_type=invoice&status=failed&status=uploaded",
        headers=auth("viewer"),
    )
    assert [d["document_id"] for d in r.json()["items"]] == [hit]


def test_page_image_signed_url(ctx: SimpleNamespace) -> None:
    """署名URLは実際にブラウザが取得できること（url が空でないだけでは不足）。

    保管先 URI（file://）を素通ししていた頃はこのアサートが無く、検証画面の帳票画像が
    壊れたまま素通りしていた。
    """
    doc_id = _upload(ctx)
    r = ctx.client.get(f"/v1/documents/{doc_id}/pages/1/image", headers=auth("viewer"))
    assert r.status_code == 200
    url = r.json()["url"]
    assert r.json()["expires_in"] == 600
    assert not url.startswith(("file://", "s3://"))  # ブラウザが読めない

    got = ctx.client.get(url.replace("http://testserver", ""))
    assert got.status_code == 200
    assert got.headers["content-type"] == "image/png"
    assert got.content == b"\x89PNG-page-1"  # FakeRasterizer が書いた実バイト


def test_page_image_content_requires_valid_token(ctx: SimpleNamespace) -> None:
    doc_id = _upload(ctx)
    r = ctx.client.get(f"/v1/documents/{doc_id}/pages/1/content?token=forged")
    assert r.status_code == 403  # E5001
    assert r.json()["error"]["code"] == "E5001"


def test_page_image_content_without_token_is_rejected(ctx: SimpleNamespace) -> None:
    doc_id = _upload(ctx)
    assert ctx.client.get(f"/v1/documents/{doc_id}/pages/1/content").status_code == 422


def test_tenant_isolation(ctx: SimpleNamespace) -> None:
    doc_id = _upload(ctx)  # ten_1
    # 別テナントのトークンでは見えない
    from gw_helpers import make_token

    other = {"Authorization": f"Bearer {make_token(role='viewer', tenant='ten_2')}"}
    r = ctx.client.get(f"/v1/documents/{doc_id}", headers=other)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "E1001"


def test_extract_rejects_empty_schema_id(ctx: SimpleNamespace) -> None:
    """空文字の schema_id は「未指定」として扱う（FK違反で 500 になっていた回帰）。"""
    doc_id = _upload(ctx)
    r = ctx.client.post(
        f"/v1/documents/{doc_id}/extract",
        headers=auth("uploader"),
        json={"schema_id": "", "options": {"force_vl": False}},
    )
    assert r.status_code == 202, r.text


def test_extract_rejects_unknown_schema_id(ctx: SimpleNamespace) -> None:
    """存在しない schema_id は 404(E1001) で明示する（従来は 500 内部エラー）。"""
    doc_id = _upload(ctx)
    r = ctx.client.post(
        f"/v1/documents/{doc_id}/extract",
        headers=auth("uploader"),
        json={"schema_id": "sch_does_not_exist", "options": {"force_vl": False}},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "E1001"
