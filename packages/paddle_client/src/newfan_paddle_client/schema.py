"""PaddleOCR サービング応答の型（付録C-1 / C-3）。

一次資料: PaddleOCR/docs/version3.x/pipeline_usage/{PP-StructureV3,OCR}.en.md の
サービング節。実サービングの録画 fixture で契約テスト（tests/）を通し、齟齬があれば
本ファイルを更新する。

方針:
- 未知フィールドは extra="allow" で温存する（サービング差分に強くする）。
- 外側エンベロープ・要素キーは camelCase、prunedResult 内は snake_case。
  camelCase のものだけ alias を付け、populate_by_name で両対応にする。
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

# rec_polys の各要素は 4 点 [[x,y],...]、bbox は [xmin,ymin,xmax,ymax]
Poly = list[list[float]]
Bbox = list[int]


class _Loose(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class OverallOcrRes(_Loose):
    rec_texts: list[str] = Field(default_factory=list)
    rec_scores: list[float] = Field(default_factory=list)
    rec_polys: list[Poly] = Field(default_factory=list)
    # OCR 単体パイプラインは軸平行 rec_boxes を返すが、StructureV3 の overall_ocr_res
    # には含まれないため Optional。含まれない場合は rec_polys から変換する（spans.py）。
    rec_boxes: Optional[list[Bbox]] = None
    dt_polys: Optional[list[Poly]] = None
    textline_orientation_angles: Optional[list[int]] = None
    # return_word_box=True 時の単語/単文字座標。サービングでの正確なフィールド名は
    # docs 未記載（付録C-3）。候補名を複数受けられるよう Optional 群で保持し、実測で確定する。
    rec_word_boxes: Optional[list[Any]] = None
    rec_words: Optional[list[Any]] = None
    text_word_boxes: Optional[list[Any]] = None


class ParsingBlock(_Loose):
    block_bbox: list[float] = Field(default_factory=list)
    block_label: str = ""
    block_content: str = ""
    block_id: Optional[int] = None
    block_order: Optional[int] = None


class TableOcrPred(_Loose):
    rec_texts: list[str] = Field(default_factory=list)
    rec_scores: list[float] = Field(default_factory=list)
    rec_polys: list[Poly] = Field(default_factory=list)
    rec_boxes: Optional[list[Bbox]] = None


class TableRes(_Loose):
    # 実 PP-StructureV3 は 4値 bbox のリスト [[x1,y1,x2,y2],...]（点列 Poly ではない）を返す。
    # 未使用フィールドかつサービング差分に強くするため Any で温存する（付録C-3, 実測で確定）。
    cell_box_list: Optional[list[Any]] = None
    pred_html: Optional[str] = None
    table_ocr_pred: Optional[TableOcrPred] = None
    block_bbox: Optional[list[float]] = None


class DocPreprocessorRes(_Loose):
    angle: Optional[int] = None


class PrunedResult(_Loose):
    parsing_res_list: list[ParsingBlock] = Field(default_factory=list)
    overall_ocr_res: Optional[OverallOcrRes] = None
    table_res_list: list[TableRes] = Field(default_factory=list)
    doc_preprocessor_res: Optional[DocPreprocessorRes] = None


class MarkdownResult(_Loose):
    text: str = ""
    images: Optional[dict[str, str]] = None
    is_start: Optional[bool] = Field(default=None, alias="isStart")
    is_end: Optional[bool] = Field(default=None, alias="isEnd")


class LayoutParsingResult(_Loose):
    pruned_result: PrunedResult = Field(alias="prunedResult", default_factory=PrunedResult)
    markdown: Optional[MarkdownResult] = None
    output_images: Optional[dict[str, str]] = Field(default=None, alias="outputImages")
    input_image: Optional[str] = Field(default=None, alias="inputImage")


class LayoutParsingResponse(_Loose):
    layout_parsing_results: list[LayoutParsingResult] = Field(
        alias="layoutParsingResults", default_factory=list
    )
    data_info: Optional[dict[str, Any]] = Field(default=None, alias="dataInfo")


class OcrResult(_Loose):
    pruned_result: OverallOcrRes = Field(alias="prunedResult", default_factory=OverallOcrRes)
    ocr_image: Optional[str] = Field(default=None, alias="ocrImage")
    doc_preprocessing_image: Optional[str] = Field(
        default=None, alias="docPreprocessingImage"
    )
    input_image: Optional[str] = Field(default=None, alias="inputImage")


class OcrResponse(_Loose):
    ocr_results: list[OcrResult] = Field(alias="ocrResults", default_factory=list)
    data_info: Optional[dict[str, Any]] = Field(default=None, alias="dataInfo")


class ServingEnvelope(_Loose):
    """PaddleX サービングの共通エンベロープ。"""

    log_id: Optional[str] = Field(default=None, alias="logId")
    error_code: Optional[int] = Field(default=None, alias="errorCode")
    error_msg: Optional[str] = Field(default=None, alias="errorMsg")
    result: dict[str, Any] = Field(default_factory=dict)
