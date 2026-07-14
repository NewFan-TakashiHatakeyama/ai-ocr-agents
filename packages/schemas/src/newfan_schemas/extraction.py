"""抽出グラフの State モデル（詳細設計 §4.2）。

Span / LayoutBlock / ExtractedField は Pydantic モデル。ExtractionState は
LangGraph 互換のため TypedDict。座標は全て前処理後画像（座標系の正、DD-01/ADR-0002）。
"""

from __future__ import annotations

from typing import Any, Optional, TypedDict

from pydantic import BaseModel, Field

from newfan_schemas.enums import ReviewStatus, SpanSource

# bbox は [x1, y1, x2, y2]（軸平行、前処理後画像座標）
BBox = list[int]


class Span(BaseModel):
    span_id: int
    page: int
    text: str
    conf: float
    bbox: BBox
    char_boxes: Optional[list[BBox]] = None
    char_confs: Optional[list[float]] = None
    source: SpanSource = SpanSource.OCR
    block_id: Optional[int] = None


class LayoutBlock(BaseModel):
    page: int
    label: str
    bbox: BBox
    content: str = ""
    span_ids: list[int] = Field(default_factory=list)
    block_order: Optional[int] = None


class ExtractedField(BaseModel):
    name: str
    label: Optional[str] = None  # 表示名（field_schemas 由来。§6.3 result / HITL UI で使用）
    value_raw: Optional[str] = None
    value_normalized: Optional[str] = None
    span_ids: list[int] = Field(default_factory=list)
    page: Optional[int] = None
    bbox: Optional[BBox] = None
    source_quote: Optional[str] = None
    confidence: float = 0.0
    grounding_score: float = 0.0
    correction: Optional[dict[str, Any]] = None
    validation: Optional[dict[str, Any]] = None
    review_status: ReviewStatus = ReviewStatus.AUTO


class TableCell(BaseModel):
    value: Optional[str] = None
    span_ids: list[int] = Field(default_factory=list)
    bbox: Optional[BBox] = None  # セル領域（§8.3 セル↔ビューア連携。構造由来 cell_box）


class TableResult(BaseModel):
    name: str
    page: Optional[int] = None
    structure_html: Optional[str] = None
    rows: list[dict[str, TableCell]] = Field(default_factory=list)
    confidence: Optional[float] = None


class ReviewItem(BaseModel):
    field_name: str
    reason: str
    confidence: float = 0.0
    critical: bool = False
    page: Optional[int] = None
    bbox: Optional[BBox] = None


class ExtractionState(TypedDict, total=False):
    """LangGraph 抽出グラフの共有 State（§4.2）。"""

    run_id: str
    document_id: str
    tenant_id: str
    schema: dict[str, Any]
    pages: list[dict[str, Any]]
    spans: list[Span]
    layout: list[LayoutBlock]
    layout_markdown: str
    fallback_pages: list[int]
    memory_examples: list[dict[str, Any]]
    active_rules: list[dict[str, Any]]
    fields: list[ExtractedField]
    tables: list[TableResult]
    review_items: list[ReviewItem]
    human_feedback: Optional[dict[str, Any]]
    errors: list[dict[str, Any]]
    metrics: dict[str, Any]
    # deterministic_normalize が field 名→正規化メタ（type_converted / confidence_cap 等）を格納。
    # confidence_score が grounding 判定・confidence 上限に利用する。
    norm_meta: dict[str, dict[str, Any]]
