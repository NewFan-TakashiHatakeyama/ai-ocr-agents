"""POST /documents/{id}/classify（⑦ 抽出UIサジェスト）。"""

from types import SimpleNamespace

from gw_helpers import PDF, auth


def _upload(ctx: SimpleNamespace, filename: str, doc_type: str | None = None) -> str:
    data = {"doc_type": doc_type} if doc_type else {}
    r = ctx.client.post(
        "/v1/documents",
        headers=auth("uploader"),
        files={"file": (filename, PDF, "application/pdf")},
        data=data,
    )
    assert r.status_code == 201, r.text
    return r.json()["document_id"]


def test_classify_declared_doc_typeを最優先する(ctx: SimpleNamespace) -> None:
    doc_id = _upload(ctx, "scan.pdf", doc_type="invoice")
    r = ctx.client.post(f"/v1/documents/{doc_id}/classify", headers=auth("viewer"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["doc_type"] == "invoice"
    assert body["method"] == "declared"
    assert body["suggested_schema_id"]
    assert body["confidence"] == 1.0


def test_classify_ファイル名から種別を推定する(ctx: SimpleNamespace) -> None:
    # doc_type 未指定・ファイル名に請求書 → invoice を提案（seed 済スキーマは invoice）
    doc_id = _upload(ctx, "請求書_ABC商事_2026.pdf")
    r = ctx.client.post(f"/v1/documents/{doc_id}/classify", headers=auth("viewer"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["doc_type"] == "invoice"
    assert body["method"] == "filename"
    assert body["confidence"] > 0.0
    assert body["suggested_schema_id"]


def test_classify_手がかりなしはサジェストしない(ctx: SimpleNamespace) -> None:
    doc_id = _upload(ctx, "0001.pdf")
    r = ctx.client.post(f"/v1/documents/{doc_id}/classify", headers=auth("viewer"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["suggested_schema_id"] is None
    assert body["doc_type"] is None
