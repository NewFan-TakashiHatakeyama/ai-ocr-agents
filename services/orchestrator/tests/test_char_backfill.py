"""DD-02 char_backfill: 低確信 span を crop→/ocr 再認識で改善する検証。"""

from __future__ import annotations

import io
from typing import Any

import pytest

pytest.importorskip("PIL")

from PIL import Image

from newfan_paddle_client import LayoutParsingResponse, OcrResponse

from newfan_orchestrator.ocr_nodes import make_structure_ocr

_LAYOUT: dict[str, Any] = {
    "layoutParsingResults": [
        {
            "prunedResult": {
                "parsing_res_list": [{"block_bbox": [100, 100, 200, 130], "block_label": "text", "block_content": "x", "block_id": 0, "block_order": 0}],
                "overall_ocr_res": {
                    "rec_texts": ["l28OOO"],  # 低確信の誤読
                    "rec_scores": [0.5],
                    "rec_polys": [[[100, 100], [200, 100], [200, 130], [100, 130]]],
                },
            },
            "markdown": {"text": "md"},
        }
    ]
}


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (500, 300), "white").save(buf, "PNG")
    return buf.getvalue()


class _FakeStructure:
    def layout_parsing(self, file_b64: str, *, file_type: int = 1) -> LayoutParsingResponse:
        return LayoutParsingResponse.model_validate(_LAYOUT)


class _FakeOcr:
    def __init__(self) -> None:
        self.calls = 0

    def ocr(self, file_b64: str, *, file_type: int = 1) -> OcrResponse:
        self.calls += 1
        return OcrResponse.model_validate(
            {"ocrResults": [{"prunedResult": {"rec_texts": ["128000"], "rec_scores": [0.95], "rec_polys": []}}]}
        )


def test_backfill_improves_low_conf_span() -> None:
    ocr = _FakeOcr()
    node = make_structure_ocr(_FakeStructure(), image_loader=lambda uri: _png(), ocr_client=ocr)
    out = node({"pages": [{"page_no": 1, "image_uri": "x"}]})

    assert ocr.calls == 1  # 低確信 span を1回 crop 再問合せ
    span = out["spans"][0]
    assert span.text == "128000" and span.conf == pytest.approx(0.95)  # 再読を採用


def test_no_backfill_when_ocr_client_absent() -> None:
    node = make_structure_ocr(_FakeStructure(), image_loader=lambda uri: _png())
    out = node({"pages": [{"page_no": 1, "image_uri": "x"}]})
    assert out["spans"][0].text == "l28OOO" and out["spans"][0].conf == pytest.approx(0.5)


def test_high_conf_span_not_requeried() -> None:
    layout = {**_LAYOUT}
    layout["layoutParsingResults"][0]["prunedResult"]["overall_ocr_res"]["rec_scores"] = [0.99]

    class _HighConf:
        def layout_parsing(self, file_b64: str, *, file_type: int = 1) -> LayoutParsingResponse:
            return LayoutParsingResponse.model_validate(layout)

    ocr = _FakeOcr()
    node = make_structure_ocr(_HighConf(), image_loader=lambda uri: _png(), ocr_client=ocr)
    node({"pages": [{"page_no": 1, "image_uri": "x"}]})
    assert ocr.calls == 0  # 閾値以上は再問合せしない
