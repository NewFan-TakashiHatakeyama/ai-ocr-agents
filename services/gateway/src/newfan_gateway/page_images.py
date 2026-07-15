"""ページ画像の署名URL発行と実体配信（§6.3 / §11 署名URL 10分）。

検証画面は帳票画像を <img src> で表示する。DB の image_uri は保管先の URI
（Local: file:///data/...、S3: s3://bucket/...）で、どちらもブラウザからは読めない。
そこで §11 の署名URLを発行する:

- S3: boto3 の事前署名 GET URL（ブラウザが直接 S3 を読む）
- Local: gateway 自身の /content エンドポイントへの URL＋短命トークン

Local のトークンは tenant/document/page/exp を含む JWT。S3 の事前署名と同じく
「対象を限定した短命の読み取り権限」で、Authorization ヘッダを付けられない
<img src> から使えるようにするためのもの。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import jwt

from newfan_gateway.errors import ApiError

_AUDIENCE = "page-image"


def issue_page_token(
    *, tenant_id: str, document_id: str, page_no: int, jwt_secret: str, jwt_alg: str, ttl_sec: int
) -> str:
    return jwt.encode(
        {
            "aud": _AUDIENCE,  # 通常の認証トークンとして使い回せないようにする
            "tenant_id": tenant_id,
            "document_id": document_id,
            "page_no": page_no,
            "exp": datetime.now(timezone.utc) + timedelta(seconds=ttl_sec),
        },
        jwt_secret,
        algorithm=jwt_alg,
    )


def verify_page_token(
    token: str, *, document_id: str, page_no: int, jwt_secret: str, jwt_alg: str
) -> str:
    """検証して tenant_id を返す。URL の document/page と一致しないトークンは拒否する。"""
    try:
        claims = jwt.decode(token, jwt_secret, algorithms=[jwt_alg], audience=_AUDIENCE)
    except jwt.PyJWTError as exc:
        raise ApiError("E5001", "画像URLの署名が無効または期限切れです") from exc
    if claims.get("document_id") != document_id or claims.get("page_no") != page_no:
        raise ApiError("E5001", "画像URLの署名が対象と一致しません")
    tenant_id = claims.get("tenant_id")
    if not tenant_id:
        raise ApiError("E5001", "画像URLの署名にテナントがありません")
    return str(tenant_id)


def presign_s3(bucket: str, key: str, *, ttl_sec: int, client: Optional[Any] = None) -> str:
    import boto3  # 遅延 import（runtime extra）

    s3 = client or boto3.client("s3")
    return str(
        s3.generate_presigned_url(
            "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=ttl_sec
        )
    )


def read_local_image(image_uri: str, *, storage_root: Path) -> bytes:
    """file:// URI の実体を読む。storage_root 配下に限定する（パストラバーサル防止）。"""
    parsed = urlparse(image_uri)
    if parsed.scheme != "file":
        raise ApiError("E2000", "ページ画像の保管先が file:// ではありません")
    # file:///data/... → /data/...（Windows の file:///C:/... も考慮して先頭 / を調整）
    raw = parsed.path
    if len(raw) > 2 and raw[0] == "/" and raw[2] == ":":
        raw = raw[1:]
    path = Path(raw).resolve()
    root = storage_root.resolve()
    if not path.is_relative_to(root):
        raise ApiError("E2000", "ページ画像が保管ルート外を指しています")
    if not path.is_file():
        raise ApiError("E1001", "ページ画像の実体が見つかりません")
    return path.read_bytes()
