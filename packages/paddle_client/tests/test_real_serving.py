"""実 PP-StructureV3 サービング出力での契約テスト（付録C-3, 実測録画 fixture）。

real_layout_parsing_sample.json は paddleocr 3.7.0（PP-StructureV3, enable_mkldnn=False）
で実文書を推論した /layout-parsing 相当の実応答。合成 fixture では露見しなかった
table_res_list.cell_box_list（実際は 4値 bbox のリスト）等の実形状で回帰を防ぐ。
"""

from __future__ import annotations

import json
from pathlib import Path

from newfan_paddle_client import (
    LayoutParsingResponse,
    build_layout_blocks,
    build_spans,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "real_layout_parsing_sample.json"


def _response() -> LayoutParsingResponse:
    data = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    return LayoutParsingResponse.model_validate(data)


def test_real_response_parses() -> None:
    resp = _response()
    assert len(resp.layout_parsing_results) == 1
    pr = resp.layout_parsing_results[0].pruned_result
    assert pr.overall_ocr_res is not None
    ocr = pr.overall_ocr_res
    # 実 OCR: 50 テキスト行、スコア・polys が対応
    assert len(ocr.rec_texts) == len(ocr.rec_scores) == len(ocr.rec_polys) >= 10


def test_real_table_cell_box_list_is_flat_bbox() -> None:
    """回帰ガード: cell_box_list は点列 Poly ではなく 4値 bbox のリスト（実測で確定）。"""
    pr = _response().layout_parsing_results[0].pruned_result
    assert pr.table_res_list, "実 fixture にはテーブルが1件含まれる"
    cells = pr.table_res_list[0].cell_box_list
    assert cells is not None and len(cells) > 0
    assert len(cells[0]) == 4  # [x1,y1,x2,y2]
    assert all(isinstance(v, (int, float)) for v in cells[0])


def test_real_spans_built_with_conf_and_bbox() -> None:
    pr = _response().layout_parsing_results[0].pruned_result
    spans = build_spans(pr, page=1)
    assert len(spans) >= 10
    assert all(len(s.bbox) == 4 and s.page == 1 for s in spans)
    # conf は実 rec_scores に一致
    assert sorted(round(s.conf, 5) for s in spans) == sorted(
        round(x, 5) for x in (pr.overall_ocr_res.rec_scores if pr.overall_ocr_res else [])
    )
    assert any(s.conf > 0.9 for s in spans)  # 高信頼 OCR が存在


def test_real_layout_blocks() -> None:
    pr = _response().layout_parsing_results[0].pruned_result
    blocks = build_layout_blocks(pr, page=1)
    assert len(blocks) >= 1
    labels = {b.label for b in blocks}
    assert labels & {"text", "table", "header", "figure_title"}
