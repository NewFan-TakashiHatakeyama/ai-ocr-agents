from types import SimpleNamespace

from gw_helpers import auth


def test_no_credentials_rejected(ctx: SimpleNamespace) -> None:
    r = ctx.client.get("/v1/documents")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "E5001"
    assert "request_id" in r.json()["error"]


def test_request_id_header_present(ctx: SimpleNamespace) -> None:
    r = ctx.client.get("/healthz")
    assert r.headers.get("X-Request-Id")


def test_viewer_cannot_upload(ctx: SimpleNamespace) -> None:
    r = ctx.client.post(
        "/v1/documents",
        headers=auth("viewer"),
        files={"file": ("a.pdf", b"%PDF-1.7\nx", "application/pdf")},
    )
    assert r.status_code == 403


def test_api_key_auth_works(ctx: SimpleNamespace) -> None:
    r = ctx.client.get("/v1/documents", headers={"X-API-Key": "api-key-123"})
    assert r.status_code == 200


def test_invalid_token_rejected(ctx: SimpleNamespace) -> None:
    r = ctx.client.get("/v1/documents", headers={"Authorization": "Bearer not-a-jwt"})
    assert r.status_code == 403


def test_wrong_secret_rejected(ctx: SimpleNamespace) -> None:
    import jwt

    bad = jwt.encode({"sub": "u", "tenant_id": "ten_1", "role": "viewer"}, "wrong", algorithm="HS256")
    r = ctx.client.get("/v1/documents", headers={"Authorization": f"Bearer {bad}"})
    assert r.status_code == 403
