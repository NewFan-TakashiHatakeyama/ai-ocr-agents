"""image_loader（s3 / file 振り分け）の単体テスト。boto3 client は Fake で注入。"""

from __future__ import annotations

import io
from pathlib import Path
from urllib.request import pathname2url

from newfan_orchestrator.image_loaders import (
    make_dispatching_image_loader,
    make_s3_image_loader,
)


class _FakeBody:
    def __init__(self, data: bytes) -> None:
        self._buf = io.BytesIO(data)

    def read(self) -> bytes:
        return self._buf.read()


class _FakeS3:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self.calls: list[tuple[str, str]] = []

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        self.calls.append((Bucket, Key))
        return {"Body": _FakeBody(self._data)}


def test_s3_loader_reads_bucket_key() -> None:
    client = _FakeS3(b"PNGDATA")
    load = make_s3_image_loader(client)
    assert load("s3://my-bucket/pre/processed/p1.png") == b"PNGDATA"
    assert client.calls == [("my-bucket", "pre/processed/p1.png")]


def test_dispatching_loader_file_scheme(tmp_path: Path) -> None:
    p = tmp_path / "img.png"
    p.write_bytes(b"LOCALPNG")
    file_uri = "file:" + pathname2url(str(p))
    load = make_dispatching_image_loader(_FakeS3(b"unused"))
    assert load(file_uri) == b"LOCALPNG"


def test_dispatching_loader_s3_scheme() -> None:
    client = _FakeS3(b"S3PNG")
    load = make_dispatching_image_loader(client)
    assert load("s3://b/k.png") == b"S3PNG"
