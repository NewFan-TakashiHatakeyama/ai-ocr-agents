"""image_loader 実装（§5.2 前処理画像の取得）。

pages.image_uri のスキームに応じて取得先を切り替える:
  s3://bucket/key → S3（boto3, runtime extra）
  file:///...      → ローカル（ocr_nodes.file_uri_loader）

worker は dispatching_image_loader を注入すれば両対応。boto3 は optional-dependency。
"""

from __future__ import annotations

from typing import Any, Callable
from urllib.parse import urlparse

from newfan_orchestrator.ocr_nodes import file_uri_loader

ImageLoader = Callable[[str], bytes]


def make_s3_image_loader(client: Any | None = None) -> ImageLoader:
    """S3 から画像バイト列を取得する image_loader を返す（client 注入でテスト可能）。"""

    def _load(uri: str) -> bytes:
        parsed = urlparse(uri)
        if parsed.scheme != "s3":
            raise ValueError(f"s3 URI ではありません: {uri}")
        c = client
        if c is None:
            import boto3

            c = boto3.client("s3")
        obj = c.get_object(Bucket=parsed.netloc, Key=parsed.path.lstrip("/"))
        return obj["Body"].read()  # type: ignore[no-any-return]

    return _load


def make_dispatching_image_loader(s3_client: Any | None = None) -> ImageLoader:
    """スキームで s3/file を振り分ける image_loader を返す。"""
    s3_load = make_s3_image_loader(s3_client)

    def _load(uri: str) -> bytes:
        if urlparse(uri).scheme == "s3":
            return s3_load(uri)
        return file_uri_loader(uri)

    return _load
