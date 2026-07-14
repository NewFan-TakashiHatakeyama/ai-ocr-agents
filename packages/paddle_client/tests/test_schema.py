"""契約テスト（付録C-1 / C-3）。

fixtures/*.json は実サービング出力の録画で置換する。置換後もこのテストが通ることで、
応答スキーマの後方互換（利用フィールドの存在）を担保する。
"""

from typing import Any

from newfan_paddle_client.schema import (
    LayoutParsingResponse,
    OcrResponse,
    ServingEnvelope,
)


def test_layout_parsing_contract(layout_parsing_raw: dict[str, Any]) -> None:
    env = ServingEnvelope.model_validate(layout_parsing_raw)
    assert env.error_code == 0

    resp = LayoutParsingResponse.model_validate(env.result)
    assert len(resp.layout_parsing_results) == 1

    page = resp.layout_parsing_results[0]
    pruned = page.pruned_result

    # 利用する主要フィールドの存在契約
    assert pruned.overall_ocr_res is not None
    assert pruned.overall_ocr_res.rec_texts
    assert len(pruned.overall_ocr_res.rec_texts) == len(pruned.overall_ocr_res.rec_scores)
    assert pruned.overall_ocr_res.rec_polys
    assert pruned.parsing_res_list[0].block_label == "doc_title"

    # 表: HTML とセル OCR
    table = pruned.table_res_list[0]
    assert table.pred_html and "<table>" in table.pred_html
    assert table.table_ocr_pred is not None

    # markdown（camelCase alias）
    assert page.markdown is not None
    assert page.markdown.is_start is True
    assert page.markdown.is_end is True

    # DD-01 の裏取り: 前処理後画像はサービング応答に無い（outputImages/inputImage は null 可）
    assert page.output_images is None
    assert page.input_image is None


def test_ocr_contract(ocr_raw: dict[str, Any]) -> None:
    env = ServingEnvelope.model_validate(ocr_raw)
    resp = OcrResponse.model_validate(env.result)
    assert len(resp.ocr_results) == 1
    pruned = resp.ocr_results[0].pruned_result
    # OCR 単体は軸平行 rec_boxes を返す（付録C-3）
    assert pruned.rec_boxes is not None
    assert pruned.rec_boxes[0] == [300, 180, 430, 212]


def test_extra_fields_preserved(layout_parsing_raw: dict[str, Any]) -> None:
    # 未知フィールドを混ぜても壊れず温存されること（サービング差分耐性）
    layout_parsing_raw["result"]["layoutParsingResults"][0]["prunedResult"][
        "future_field"
    ] = {"x": 1}
    env = ServingEnvelope.model_validate(layout_parsing_raw)
    resp = LayoutParsingResponse.model_validate(env.result)
    dumped = resp.layout_parsing_results[0].pruned_result.model_dump()
    assert dumped["future_field"] == {"x": 1}
