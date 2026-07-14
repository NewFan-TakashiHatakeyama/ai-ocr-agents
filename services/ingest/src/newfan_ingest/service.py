"""取込オーケストレーション（§5.1 → §5.2）。

validate → 原本保存 → ラスタライズ → 前処理（座標系の正を確定）→ ページ保存。
外部依存（ObjectStore/Rasterizer/Preprocessor）は DI で受ける（テスト容易性）。
"""

from __future__ import annotations

from newfan_ingest.errors import IngestError
from newfan_ingest.models import IngestResult, PageImage, UploadInput
from newfan_ingest.preprocess import NoopPreprocessor, Preprocessor
from newfan_ingest.rasterize import Rasterizer
from newfan_ingest.storage import ObjectStore, original_key, page_key
from newfan_ingest.validation import IngestLimits, validate_upload

_KIND_TO_EXT = {"pdf": "pdf", "png": "png", "jpeg": "jpg", "tiff": "tiff", "office": "pdf"}
_KIND_TO_MIME = {
    "pdf": "application/pdf",
    "png": "image/png",
    "jpeg": "image/jpeg",
    "tiff": "image/tiff",
    "office": "application/pdf",
}


class IngestService:
    def __init__(
        self,
        store: ObjectStore,
        rasterizer: Rasterizer,
        preprocessor: Preprocessor | None = None,
        limits: IngestLimits | None = None,
    ) -> None:
        self._store = store
        self._rasterizer = rasterizer
        self._preprocessor = preprocessor or NoopPreprocessor()
        self._limits = limits or IngestLimits()

    def ingest(self, upload: UploadInput, *, source_hint: str = "unknown") -> IngestResult:
        kind = validate_upload(upload.content, upload.filename, self._limits)

        raster_input = upload.content
        raster_kind = kind
        if kind == "office":
            # LibreOffice headless で PDF 化（失敗は E1003）。ここでは未実装を明示。
            raise IngestError("E1003", "Office→PDF 変換は未実装（LibreOffice 連携を差し込む）")

        # 原本保存
        ext = _KIND_TO_EXT[kind]
        storage_uri = self._store.put(
            original_key(upload.tenant_id, upload.document_id, ext),
            upload.content,
            _KIND_TO_MIME[kind],
        )

        # ラスタライズ
        raster_pages = self._rasterizer.rasterize(raster_input, raster_kind)
        if len(raster_pages) > self._limits.max_pages:
            raise IngestError("E1002", f"ページ数が上限 {self._limits.max_pages} を超えています")

        # 前処理（座標系の正を確定）→ ページ保存
        pages: list[PageImage] = []
        for rp in raster_pages:
            pp = self._preprocessor.preprocess(rp, source_hint=source_hint)
            uri = self._store.put(
                page_key(upload.tenant_id, upload.document_id, pp.page_no),
                pp.png_bytes,
                "image/png",
            )
            pages.append(
                PageImage(
                    page_no=pp.page_no,
                    width=pp.width,
                    height=pp.height,
                    image_uri=uri,
                    preproc=pp.preproc,
                )
            )

        return IngestResult(
            document_id=upload.document_id,
            storage_uri=storage_uri,
            mime_type=_KIND_TO_MIME[kind],
            page_count=len(pages),
            pages=pages,
        )
