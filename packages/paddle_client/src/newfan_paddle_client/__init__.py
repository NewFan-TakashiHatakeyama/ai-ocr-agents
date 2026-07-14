"""PaddleOCR サービングクライアント（§5.3 / 付録C-1, C-3）。"""

from newfan_paddle_client.client import PaddleServingClient, encode_image
from newfan_paddle_client.errors import PaddleServingError
from newfan_paddle_client.schema import (
    LayoutParsingResponse,
    LayoutParsingResult,
    OcrResponse,
    OcrResult,
    OverallOcrRes,
    PrunedResult,
    ServingEnvelope,
)
from newfan_paddle_client.spans import build_layout_blocks, build_spans, poly_to_bbox

__all__ = [
    "PaddleServingClient",
    "encode_image",
    "PaddleServingError",
    "LayoutParsingResponse",
    "LayoutParsingResult",
    "OcrResponse",
    "OcrResult",
    "OverallOcrRes",
    "PrunedResult",
    "ServingEnvelope",
    "build_spans",
    "build_layout_blocks",
    "poly_to_bbox",
]
