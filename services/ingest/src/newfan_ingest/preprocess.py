"""前処理（§5.2, DD-01/ADR-0002）。

ADR-0002 の決定により、向き補正・アンワープは ingest 側で行い、その PNG を
「座標系の正」とする。structure/ocr サービングは前処理オフで呼ぶ。

判定方針（§5.2）:
- スキャナ由来（EXIF なし・歪み小）: unwarp=False
- スマホ撮影疑い（EXIF あり or 台形歪み検知）: unwarp=True

MVP 初期は orientation のみでも可（unwarp は段階導入）。実装は PaddleOCR の
doc_preprocessor パイプライン単体呼び出し、または軽量な自前回転を差し込む。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from newfan_ingest.rasterize import RasterPage


@dataclass(frozen=True)
class PreprocessedPage:
    page_no: int
    width: int
    height: int
    png_bytes: bytes
    # pages.preproc に保存: {"angle": 0, "unwarp": false, "scale": 1.0}
    preproc: dict[str, object] = field(default_factory=dict)


class Preprocessor(Protocol):
    def preprocess(self, page: RasterPage, *, source_hint: str) -> PreprocessedPage:
        """1 ページを前処理し、前処理後 PNG とメタを返す。source_hint: scanner|photo|unknown。"""
        ...


class NoopPreprocessor:
    """前処理なし（スキャナ由来・整った画像向け。MVP フォールバック）。"""

    def preprocess(self, page: RasterPage, *, source_hint: str = "unknown") -> PreprocessedPage:
        return PreprocessedPage(
            page_no=page.page_no,
            width=page.width,
            height=page.height,
            png_bytes=page.png_bytes,
            preproc={"angle": 0, "unwarp": False, "scale": 1.0},
        )
