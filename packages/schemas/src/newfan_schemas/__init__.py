"""NewFan AI-OCR ドメインモデル・API型。

詳細設計 §4.2（State 定義）・§5.5（フィールドスキーマ）に対応する Pydantic モデル。
OpenAPI 生成元・サービス間共有型の単一情報源。
"""

from newfan_schemas.extraction import (
    ExtractedField,
    ExtractionState,
    LayoutBlock,
    ReviewItem,
    Span,
    TableCell,
    TableResult,
)
from newfan_schemas.field_schema import (
    MIN_REGION_AREA,
    ColumnDef,
    FieldDef,
    FieldSchema,
    RegionRect,
    resolve_page,
    resolve_regions,
)
from newfan_schemas.enums import DocStatus, FieldType, ReviewStatus, RunStatus, SpanSource

__all__ = [
    "Span",
    "LayoutBlock",
    "ExtractedField",
    "TableResult",
    "TableCell",
    "ReviewItem",
    "ExtractionState",
    "FieldSchema",
    "FieldDef",
    "ColumnDef",
    "RegionRect",
    "MIN_REGION_AREA",
    "resolve_page",
    "resolve_regions",
    "FieldType",
    "SpanSource",
    "ReviewStatus",
    "RunStatus",
    "DocStatus",
]
