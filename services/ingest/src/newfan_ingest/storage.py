"""オブジェクトストレージ抽象（§2.3 パス規約）。

本番は S3（SSE-KMS）。dev/テストは LocalObjectStore（ファイルシステム）。
パス規約: {tenant_id}/{document_id}/original.{ext}, .../pages/{page_no}.png
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class ObjectStore(Protocol):
    def put(self, key: str, data: bytes, content_type: str) -> str:
        """key に data を保存し、URI（s3://... 等）を返す。"""
        ...


class LocalObjectStore:
    """ファイルシステム実装（dev/結合テスト用）。URI は file:// を返す。"""

    def __init__(self, root: Path) -> None:
        self._root = root

    def put(self, key: str, data: bytes, content_type: str) -> str:
        path = self._root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path.resolve().as_uri()


class S3ObjectStore:
    """本番実装（S3 SSE-KMS, §2.3）。runtime extra（boto3）が必要。"""

    def __init__(self, bucket: str, *, kms_key_id: str | None = None, client: object | None = None) -> None:
        if client is not None:
            self._client = client
        else:
            import boto3  # 遅延 import（runtime extra）

            self._client = boto3.client("s3")
        self._bucket = bucket
        self._kms_key_id = kms_key_id

    def put(self, key: str, data: bytes, content_type: str) -> str:
        extra: dict[str, str] = {"ContentType": content_type}
        if self._kms_key_id:
            extra["ServerSideEncryption"] = "aws:kms"
            extra["SSEKMSKeyId"] = self._kms_key_id
        else:
            extra["ServerSideEncryption"] = "aws:kms"
        self._client.put_object(Bucket=self._bucket, Key=key, Body=data, **extra)  # type: ignore[attr-defined]
        return f"s3://{self._bucket}/{key}"


def original_key(tenant_id: str, document_id: str, ext: str) -> str:
    return f"{tenant_id}/{document_id}/original.{ext.lstrip('.')}"


def page_key(tenant_id: str, document_id: str, page_no: int) -> str:
    return f"{tenant_id}/{document_id}/pages/{page_no}.png"
