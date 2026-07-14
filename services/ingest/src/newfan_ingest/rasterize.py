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
