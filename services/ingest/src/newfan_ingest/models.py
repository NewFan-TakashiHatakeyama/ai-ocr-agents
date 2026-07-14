from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class UploadInput(BaseModel):
    tenant_id: str
    document_id: str
    filename: str
    content: bytes
    declared_mime: Optional[str] = None
    doc_type: Optional[str] = None
    external_ref: Optional[str] = None


class PageImage(BaseModel):
    """前処理後ページ画像（座標系の正, DD-01/ADR-0002）。"""

    page_no: int
    width: int
    height: int
    image_uri: str
    # 回転角/unwarp 有無/縮小倍率（pages.preproc）
    preproc: dict[str, object] = Field(default_factory=dict)


class IngestResult(BaseModel):
    document_id: str
    storage_uri: str  # 原本の保存先（s3://.../original.ext）
    mime_type: str
    page_count: int
    pages: list[PageImage] = Field(default_factory=list)
