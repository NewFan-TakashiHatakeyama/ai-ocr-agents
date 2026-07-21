"""ラスタライズ（§5.1）。画像/TIFF は Pillow、PDF は pypdfium2、振り分けは AutoRasterizer。

PdfiumRasterizer だけを注入すると PNG アップロードが 500 になる回帰を防ぐ
（validate_upload は pdf/png/jpeg/tiff/office を通すため）。
"""

from __future__ import annotations

import io

import pytest

from newfan_ingest.rasterize import AutoRasterizer, PillowRasterizer

Image = pytest.importorskip("PIL.Image", reason="Pillow は runtime extra")


def _png(w: int = 40, h: int = 30, color: str = "red") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


def _multipage_tiff(pages: int = 3) -> bytes:
    imgs = [Image.new("RGB", (20 + i, 10 + i), "blue") for i in range(pages)]
    buf = io.BytesIO()
    imgs[0].save(buf, format="TIFF", save_all=True, append_images=imgs[1:])
    return buf.getvalue()


def test_png_is_single_page() -> None:
    pages = PillowRasterizer().rasterize(_png(40, 30), "png")
    assert len(pages) == 1
    assert (pages[0].page_no, pages[0].width, pages[0].height) == (1, 40, 30)
    assert pages[0].png_bytes.startswith(b"\x89PNG")


def test_tiff_is_split_per_page() -> None:
    pages = PillowRasterizer().rasterize(_multipage_tiff(3), "tiff")
    assert [p.page_no for p in pages] == [1, 2, 3]
    # 各ページが個別のサイズで PNG 化される
    assert [p.width for p in pages] == [20, 21, 22]
    assert all(p.png_bytes.startswith(b"\x89PNG") for p in pages)


def test_grayscale_is_normalized_to_png() -> None:
    """CMYK/グレースケール等も PNG へ正規化する（OCR 前段の統一）。"""
    buf = io.BytesIO()
    Image.new("L", (12, 8), 128).save(buf, format="PNG")
    pages = PillowRasterizer().rasterize(buf.getvalue(), "png")
    assert len(pages) == 1 and pages[0].width == 12


def test_pillow_rejects_pdf() -> None:
    with pytest.raises(NotImplementedError):
        PillowRasterizer().rasterize(b"%PDF-1.7", "pdf")


def test_auto_routes_image_to_pillow() -> None:
    """AutoRasterizer が画像を Pillow 側へ振り分ける（PDF は pypdfium2 に委譲）。"""
    pages = AutoRasterizer().rasterize(_png(50, 20), "png")
    assert len(pages) == 1 and pages[0].width == 50
