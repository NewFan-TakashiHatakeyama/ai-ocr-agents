"""確定 JSON の保存先（§5.9）。本番は S3、dev/テストは Local。"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class ObjectStore(Protocol):
    def put(self, key: str, data: bytes, content_type: str) -> str: ...


class LocalObjectStore:
    def __init__(self, root: Path) -> None:
        self._root = root

    def put(self, key: str, data: bytes, content_type: str) -> str:
        path = self._root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path.resolve().as_uri()


class S3ObjectStore:
    """本番実装（S3 SSE-KMS）。runtime extra（boto3）が必要。"""

    def __init__(self, bucket: str, *, kms_key_id: str | None = None, client: object | None = None) -> None:
        if client is not None:
            self._client = client
        else:
            import boto3

            self._client = boto3.client("s3")
        self._bucket = bucket
        self._kms_key_id = kms_key_id

    def put(self, key: str, data: bytes, content_type: str) -> str:
        extra: dict[str, str] = {"ContentType": content_type, "ServerSideEncryption": "aws:kms"}
        if self._kms_key_id:
            extra["SSEKMSKeyId"] = self._kms_key_id
        self._client.put_object(Bucket=self._bucket, Key=key, Body=data, **extra)  # type: ignore[attr-defined]
        return f"s3://{self._bucket}/{key}"


def canonical_key(tenant_id: str, document_id: str, run_id: str) -> str:
    return f"{tenant_id}/{document_id}/derived/{run_id}.json"
