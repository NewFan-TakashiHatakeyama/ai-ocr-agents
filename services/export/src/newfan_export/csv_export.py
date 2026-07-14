"""CSV 生成（§5.9）。テナントのマッピング設定に従い flatten。明細は別 CSV（親 document_id 付き）。"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from typing import Optional

from newfan_export.models import ExportInput


@dataclass(frozen=True)
class CsvColumn:
    csv_name: str
    field_name: str


@dataclass(frozen=True)
class CsvMapping:
    """列名・順序（§5.9）。未指定時はフィールド名をそのまま列にする。"""

    columns: list[CsvColumn]


def _final_value(inp: ExportInput, field_name: str) -> str:
    for f in inp.fields:
        if f.name == field_name:
            return f.value_normalized or ""
    return ""


def to_main_csv(inp: ExportInput, mapping: Optional[CsvMapping] = None) -> str:
    if mapping is None:
        cols = [CsvColumn(f.name, f.name) for f in inp.fields]
    else:
        cols = mapping.columns

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["document_id"] + [c.csv_name for c in cols])
    writer.writerow([inp.document_id] + [_final_value(inp, c.field_name) for c in cols])
    return buf.getvalue()


def to_line_items_csv(inp: ExportInput, *, table_name: str = "line_items") -> str:
    table = next((t for t in inp.tables if t.name == table_name), None)
    if table is None or not table.rows:
        return ""

    cols: list[str] = []
    for row in table.rows:
        for key in row:
            if key not in cols:
                cols.append(key)

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["document_id"] + cols)
    for row in table.rows:
        values = [(row[c].value or "" if c in row else "") for c in cols]
        writer.writerow([inp.document_id] + values)
    return buf.getvalue()
