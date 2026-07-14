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


def original_key(tenant_id: str, document_id: str, ext: str) -> str:
    return f"{tenant_id}/{document_id}/original.{ext.lstrip('.')}"


def page_key(tenant_id: str, document_id: str, page_no: int) -> str:
    return f"{tenant_id}/{document_id}/pages/{page_no}.png"
