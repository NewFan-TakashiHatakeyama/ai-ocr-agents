"""抽出スキーマ定義（詳細設計 §5.5）。field_schemas.fields の形式。"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from newfan_schemas.enums import FieldType


class ColumnDef(BaseModel):
    name: str
    type: FieldType = FieldType.STRING
    label: Optional[str] = None


class FieldDef(BaseModel):
    name: str
    label: Optional[str] = None
    type: FieldType = FieldType.STRING
    required: bool = False
    critical: bool = False
    columns: Optional[list[ColumnDef]] = None


class FieldSchema(BaseModel):
    doc_type: str
    fields: list[FieldDef] = Field(default_factory=list)

    def critical_field_names(self) -> set[str]:
        return {f.name for f in self.fields if f.critical}
