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
# **float で受ける**。PP-StructureV3 は解像度によって座標を小数で返す
# （実測: PDF を高 DPI でラスタライズしたページで rec_boxes に 2033.13… が出る）。
# ここを int にしていたため pydantic の int_from_float で**応答全体が検証エラーになり、
# ページが丸ごと欠落**していた（ocr_nodes はページ単位で例外を握って継続するので、
# 抽出は「成功」のまま結果だけが減る）。整数化は座標を使う側（spans / tables）が行う。
Bbox = list[float]


class _Loose(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class OverallOcrRes(_Loose):
    rec_texts: list[str] = Field(default_factory=list)
    rec_scores: list[float] = Field(default_factory=list)
    rec_polys: list[Poly] = Field(default_factory=list)
    # 実測(paddleocr 3.7.0 PP-StructureV3): overall_ocr_res に rec_boxes（軸平行bbox）が
    # 含まれる。旧構成で欠落する場合に備え Optional とし、無ければ rec_polys から変換（spans.py）。
    rec_boxes: Optional[list[Bbox]] = None
    dt_polys: Optional[list[Poly]] = None
    textline_orientation_angles: Optional[list[int]] = None
    # 付録C-3 実測結論: PP-StructureV3 + PP-OCRv5_server_rec では return_word_box=True を
    # 指定しても単語/単文字座標は overall_ocr_res に出力されない（rec_word_boxes/rec_words/
    # text_word_boxes は全て ABSENT。得られるのは行レベル rec_polys/rec_boxes のみ）。
    # → 文字単位グラウンディング（char_boxes/char_confs, §8.3 文字差分ポップオーバー）は
    #   DD-02 の crop→/ocr 再問合せで補完する必要がある。下記は他構成向けの前方互換で温存。
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
