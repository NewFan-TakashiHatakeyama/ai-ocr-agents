from typing import Any

from newfan_schemas import SpanSource

from newfan_paddle_client.schema import LayoutParsingResponse, ServingEnvelope
from newfan_paddle_client.spans import build_layout_blocks, build_spans, poly_to_bbox


def test_poly_to_bbox() -> None:
    assert poly_to_bbox([[10, 20], [110, 20], [110, 46], [10, 46]]) == [10, 20, 110, 46]
    # 傾いたポリゴンでも外接矩形を返す
    assert poly_to_bbox([[12, 18], [110, 22], [108, 48], [10, 44]]) == [10, 18, 110, 48]


def test_build_spans_reading_order(layout_parsing_raw: dict[str, Any]) -> None:
    resp = LayoutParsingResponse.model_validate(
        ServingEnvelope.model_validate(layout_parsing_raw).result
    )
    pruned = resp.layout_parsing_results[0].pruned_result

    spans = build_spans(pruned, page=1, start_id=0)
    assert [s.text for s in spans] == ["請求書", "株式会社サンプル御中", "御請求金額", "¥128,000"]
    # span_id は連番、bbox は rec_polys 由来
    assert [s.span_id for s in spans] == [0, 1, 2, 3]
    assert spans[3].bbox == [300, 180, 430, 212]
    assert spans[3].conf == 0.734
    assert all(s.source is SpanSource.OCR for s in spans)


def test_build_spans_start_id_offset(layout_parsing_raw: dict[str, Any]) -> None:
    resp = LayoutParsingResponse.model_validate(
        ServingEnvelope.model_validate(layout_parsing_raw).result
    )
    pruned = resp.layout_parsing_results[0].pruned_result
    spans = build_spans(pruned, page=2, start_id=100)
    assert [s.span_id for s in spans] == [100, 101, 102, 103]
    assert all(s.page == 2 for s in spans)


def test_build_layout_blocks(layout_parsing_raw: dict[str, Any]) -> None:
    resp = LayoutParsingResponse.model_validate(
        ServingEnvelope.model_validate(layout_parsing_raw).result
    )
    pruned = resp.layout_parsing_results[0].pruned_result
    blocks = build_layout_blocks(pruned, page=1)
    assert [b.label for b in blocks] == ["doc_title", "text", "table"]
    assert blocks[0].bbox == [40, 40, 520, 90]


def test_build_spans_empty_ocr() -> None:
    from newfan_paddle_client.schema import PrunedResult

    assert build_spans(PrunedResult(), page=1) == []
