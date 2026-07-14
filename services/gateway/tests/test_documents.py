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


def test_page_image_signed_url(ctx: SimpleNamespace) -> None:
    doc_id = _upload(ctx)
    r = ctx.client.get(f"/v1/documents/{doc_id}/pages/1/image", headers=auth("viewer"))
    assert r.status_code == 200
    assert r.json()["url"]
    assert r.json()["expires_in"] == 600


def test_tenant_isolation(ctx: SimpleNamespace) -> None:
    doc_id = _upload(ctx)  # ten_1
    # 別テナントのトークンでは見えない
    from gw_helpers import make_token

    other = {"Authorization": f"Bearer {make_token(role='viewer', tenant='ten_2')}"}
    r = ctx.client.get(f"/v1/documents/{doc_id}", headers=other)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "E1001"
