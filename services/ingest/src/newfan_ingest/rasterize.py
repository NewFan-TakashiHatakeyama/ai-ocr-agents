"""ページ分割・ラスタライズ（§5.1）。

PDF→画像は pypdfium2 で 250dpi。TIFF はページ分割し PNG 化。実装は重い依存を
遅延 import する。テストは RasterizerProtocol の fake を使う。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class RasterPage:
    page_no: int
    width: int
    height: int
    png_bytes: bytes


class Rasterizer(Protocol):
    def rasterize(self, content: bytes, kind: str) -> list[RasterPage]:
        """原本バイト列を PNG ページ列へ。ページ数超過等の判定は呼び出し側。"""
        ...


class PillowRasterizer:
    """画像（png/jpeg）・TIFF の実装。実行環境（runtime extra）でのみ利用可能。

    - png/jpeg: 1 ページとして正規化（RGB 化して PNG 再エンコード）。
    - tiff: マルチページを分割して各ページを PNG 化（§5.1）。
    PDF は扱わない（PdfiumRasterizer の担当）。
    """

    _KINDS = ("png", "jpeg", "tiff")

    def rasterize(self, content: bytes, kind: str) -> list[RasterPage]:
        if kind not in self._KINDS:
            raise NotImplementedError(
                f"PillowRasterizer は {self._KINDS} のみ対応（kind={kind!r}）。PDF は PdfiumRasterizer。"
            )
        import io

        from PIL import Image, ImageSequence  # 遅延 import（runtime extra）

        pages: list[RasterPage] = []
        with Image.open(io.BytesIO(content)) as im:
            for i, frame in enumerate(ImageSequence.Iterator(im)):
                # PNG 化のため RGB へ正規化（CMYK/パレット/16bit グレースケール等を吸収）
                rgb = frame.convert("RGB")
                buf = io.BytesIO()
                rgb.save(buf, format="PNG")
                pages.append(
                    RasterPage(
                        page_no=i + 1,
                        width=rgb.width,
                        height=rgb.height,
                        png_bytes=buf.getvalue(),
                    )
                )
        return pages


class AutoRasterizer:
    """kind に応じて PDF / 画像の実装へ振り分ける（本番の既定）。

    gateway/ingest は pdf・png・jpeg・tiff・office を受け付ける（validate_upload）ため、
    PdfiumRasterizer 単体を注入すると画像アップロードが NotImplementedError で 500 になる。
    """

    def __init__(self, dpi: int = 250) -> None:
        self._pdf = PdfiumRasterizer(dpi=dpi)
        self._img = PillowRasterizer()

    def rasterize(self, content: bytes, kind: str) -> list[RasterPage]:
        if kind == "pdf":
            return self._pdf.rasterize(content, kind)
        return self._img.rasterize(content, kind)


class PdfiumRasterizer:
    """pypdfium2 実装。実行環境（runtime extra）でのみ利用可能。"""

    def __init__(self, dpi: int = 250) -> None:
        self._dpi = dpi

    def rasterize(self, content: bytes, kind: str) -> list[RasterPage]:
        if kind != "pdf":
            raise NotImplementedError(
                "PdfiumRasterizer は PDF のみ対応。画像/TIFF は Pillow 実装を使うこと。"
            )
        import pypdfium2 as pdfium  # 遅延 import（runtime extra）

        scale = self._dpi / 72.0
        pdf = pdfium.PdfDocument(content)
        pages: list[RasterPage] = []
        try:
            for i in range(len(pdf)):
                page = pdf[i]
                bitmap = page.render(scale=scale)
                pil = bitmap.to_pil()
                import io

                buf = io.BytesIO()
                pil.save(buf, format="PNG")
                pages.append(
                    RasterPage(
                        page_no=i + 1,
                        width=pil.width,
                        height=pil.height,
                        png_bytes=buf.getvalue(),
                    )
                )
        finally:
            pdf.close()
        return pages
