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
def test_list_and_get_expose_original_name(ctx: SimpleNamespace) -> None:
    """一覧・単体の両方が原本ファイル名（original_name）を返す。

    以前は一覧が document_id しか返さず、チャットやフォルダ監視で取り込んだ帳票を
    利用者が見分けられなかった。web の一覧の表示名・検索はこの値に依存する。
    """
    r = ctx.client.post(
        "/v1/documents",
        headers=auth("uploader"),
        files={"file": ("請求書_2026-09_ACME.pdf", PDF, "application/pdf")},
    )
    assert r.status_code == 201, r.text
    doc_id = r.json()["document_id"]

    listed = ctx.client.get("/v1/documents", headers=auth("viewer")).json()["items"]
    assert [d["original_name"] for d in listed if d["document_id"] == doc_id] == [
        "請求書_2026-09_ACME.pdf"
    ]
    single = ctx.client.get(f"/v1/documents/{doc_id}", headers=auth("viewer")).json()
    assert single["original_name"] == "請求書_2026-09_ACME.pdf"


def test_original_name_is_null_when_unknown(ctx: SimpleNamespace) -> None:
    """名前を持たない行（旧データ・外部投入）は null で返し、一覧を落とさない。"""
    from newfan_gateway.records import DocumentRecord

    ctx.repo.create_document(
        DocumentRecord(
            id="doc_noname",
            tenant_id="ten_1",
            storage_uri="file:///x",
            mime_type="application/pdf",
            page_count=1,
        ),
        [],
    )
    r = ctx.client.get("/v1/documents", headers=auth("viewer"))
    assert r.status_code == 200
    items = {d["document_id"]: d for d in r.json()["items"]}
    assert items["doc_noname"]["original_name"] is None


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
