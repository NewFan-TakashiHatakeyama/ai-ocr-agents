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


def canonical_key(tenant_id: str, document_id: str, run_id: str) -> str:
    return f"{tenant_id}/{document_id}/derived/{run_id}.json"
