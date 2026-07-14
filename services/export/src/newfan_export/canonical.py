"""canonical JSON 生成（§5.9 / §6.3 result 形式 ＋ 確定値）。"""

from __future__ import annotations

from typing import Any

from newfan_schemas import ExtractedField

from newfan_export.models import ExportInput


def _field_json(field: ExtractedField) -> dict[str, Any]:
    d = field.model_dump(mode="json")
    # 確定値（HITL 後）。DB の final_value を持たない場合は value_normalized を採用。
    d["final"] = field.value_normalized
    return d


def build_canonical_json(inp: ExportInput) -> dict[str, Any]:
    return {
        "document_id": inp.document_id,
        "run_id": inp.run_id,
        "status": inp.status,
        "external_ref": inp.external_ref,
        "engine_versions": inp.engine_versions,
        "fields": [_field_json(f) for f in inp.fields],
        "tables": [t.model_dump(mode="json") for t in inp.tables],
        "review_summary": inp.review_summary,
    }
