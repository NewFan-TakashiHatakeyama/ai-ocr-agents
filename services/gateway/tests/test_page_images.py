"""ページ画像の署名URL（§6.3 / §11）。

検証画面は帳票画像を <img src> で表示する。保管先 URI（file:// / s3://）を素通しすると
ブラウザが読めず画像が出ない回帰を防ぐ（dev seed は data: URI だったため、実アップロード
経路でしか露見しなかった）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from newfan_gateway.errors import ApiError
from newfan_gateway.page_images import (
    issue_page_token,
    presign_s3,
    read_local_image,
    verify_page_token,
)

SECRET = "test-secret-0123456789"
ALG = "HS256"


def _token(**over: object) -> str:
    kw: dict = {
        "tenant_id": "ten_1",
        "document_id": "doc_a",
        "page_no": 1,
        "jwt_secret": SECRET,
        "jwt_alg": ALG,
        "ttl_sec": 600,
    }
    kw.update(over)
    return issue_page_token(**kw)  # type: ignore[arg-type]


def test_token_roundtrip_returns_tenant() -> None:
    tenant = verify_page_token(
        _token(), document_id="doc_a", page_no=1, jwt_secret=SECRET, jwt_alg=ALG
    )
    assert tenant == "ten_1"


def test_token_for_other_document_is_rejected() -> None:
    """URL を差し替えて他文書の画像を読めないこと。"""
    with pytest.raises(ApiError):
        verify_page_token(_token(), document_id="doc_b", page_no=1, jwt_secret=SECRET, jwt_alg=ALG)


def test_token_for_other_page_is_rejected() -> None:
    with pytest.raises(ApiError):
        verify_page_token(_token(), document_id="doc_a", page_no=2, jwt_secret=SECRET, jwt_alg=ALG)


def test_token_signed_with_other_secret_is_rejected() -> None:
    forged = _token(jwt_secret="attacker-secret")
    with pytest.raises(ApiError):
        verify_page_token(forged, document_id="doc_a", page_no=1, jwt_secret=SECRET, jwt_alg=ALG)


def test_expired_token_is_rejected() -> None:
    with pytest.raises(ApiError):
        verify_page_token(
            _token(ttl_sec=-1), document_id="doc_a", page_no=1, jwt_secret=SECRET, jwt_alg=ALG
        )


def test_normal_bearer_token_cannot_be_used_as_page_token() -> None:
    """aud=page-image を持たない通常の認証トークンは画像URLに流用できない。"""
    import jwt as pyjwt

    bearer = pyjwt.encode({"sub": "u", "tenant_id": "ten_1", "role": "admin"}, SECRET, ALG)
    with pytest.raises(ApiError):
        verify_page_token(bearer, document_id="doc_a", page_no=1, jwt_secret=SECRET, jwt_alg=ALG)


def test_read_local_image_returns_bytes(tmp_path: Path) -> None:
    f = tmp_path / "ten_1" / "doc_a" / "pages" / "1.png"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"\x89PNG-data")
    assert read_local_image(f.as_uri(), storage_root=tmp_path) == b"\x89PNG-data"


def test_read_local_image_rejects_path_outside_root(tmp_path: Path) -> None:
    outside = tmp_path.parent / "secret.png"
    outside.write_bytes(b"x")
    root = tmp_path / "storage"
    root.mkdir()
    with pytest.raises(ApiError):
        read_local_image(outside.as_uri(), storage_root=root)


def test_read_local_image_rejects_non_file_scheme(tmp_path: Path) -> None:
    with pytest.raises(ApiError):
        read_local_image("s3://bucket/ten_1/doc_a/pages/1.png", storage_root=tmp_path)


def test_presign_s3_uses_bucket_key_and_ttl() -> None:
    captured: dict = {}

    class _FakeS3:
        def generate_presigned_url(self, op: str, *, Params: dict, ExpiresIn: int) -> str:
            captured.update(op=op, params=Params, ttl=ExpiresIn)
            return "https://s3.example.com/signed"

    url = presign_s3("bkt", "ten_1/doc_a/pages/1.png", ttl_sec=600, client=_FakeS3())
    assert url == "https://s3.example.com/signed"
    assert captured == {
        "op": "get_object",
        "params": {"Bucket": "bkt", "Key": "ten_1/doc_a/pages/1.png"},
        "ttl": 600,
    }


def test_presign_s3_client_uses_region_and_sigv4(monkeypatch: pytest.MonkeyPatch) -> None:
    """client 未注入時は region と SigV4 を明示すること。

    既定の boto3.client("s3") はグローバルエンドポイント宛の SigV2 URL を作り、
    ap-northeast-1（SigV4 のみ）では S3 が TemporaryRedirect を返して画像が出ない。
    ECS のタスクロールは一時認証情報なので、SigV4 で security token を含める必要もある。
    """
    boto3 = pytest.importorskip("boto3", reason="boto3 は runtime extra")
    captured: dict = {}

    def _fake_client(service: str, *, region_name: object = None, config: object = None) -> object:
        captured.update(service=service, region=region_name, config=config)

        class _S:
            def generate_presigned_url(self, op: str, *, Params: dict, ExpiresIn: int) -> str:
                return "https://signed"

        return _S()

    monkeypatch.setattr(boto3, "client", _fake_client)
    monkeypatch.setenv("AWS_REGION", "ap-northeast-1")
    presign_s3("bkt", "k", ttl_sec=60)

    assert captured["service"] == "s3"
    assert captured["region"] == "ap-northeast-1"
    assert captured["config"].signature_version == "s3v4"
