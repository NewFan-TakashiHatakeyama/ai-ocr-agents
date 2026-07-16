"""KIE 抽出（§4.6.1 / §5.5）。

LLM に span_ids 必須の JSON 契約で抽出させ、**span_ids の実在をコードで検証**する
（原文にない値・推測値を弾く安全策）。value_raw は LLM の value、source_quote は
実在 span テキストの連結（grounding 判定の根拠）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from newfan_schemas import ExtractedField, Span, TableCell, TableResult

from newfan_llm_adapter.adapter import LLMAdapter
from newfan_llm_adapter.bundle import PromptBundle, render
from newfan_llm_adapter.provider import LLMResponse


@dataclass
class KieResult:
    fields: list[ExtractedField] = field(default_factory=list)
    tables: list[TableResult] = field(default_factory=list)
    unmapped_required: list[str] = field(default_factory=list)
    response: LLMResponse | None = None


def _spans_for_prompt(spans: list[Span]) -> str:
    return json.dumps(
        [{"span_id": s.span_id, "page": s.page, "text": s.text, "conf": round(s.conf, 3)} for s in spans],
        ensure_ascii=False,
    )


def _valid_span_ids(raw: Any, span_map: dict[int, Span]) -> list[int]:
    out: list[int] = []
    if isinstance(raw, list):
        for x in raw:
            if isinstance(x, int) and x in span_map:
                out.append(x)
    return out


def kie_extract(
    adapter: LLMAdapter,
    bundle: PromptBundle,
    *,
    spans: list[Span],
    layout_markdown: str,
    schema_json: dict[str, Any],
    rule_hints: str = "",
) -> KieResult:
    span_map = {s.span_id: s for s in spans}
    # スキーマから name→label を引く（HITL UI / §6.3 result の表示名）
    label_map = {
        f.get("name"): f.get("label")
        for f in (schema_json.get("fields") or [])
        if isinstance(f, dict)
    }
    system = "あなたは帳票からの項目抽出エンジンです。出力はJSONのみ。"
    user = render(
        bundle.kie_template,
        {
            "layout_markdown": layout_markdown,
            "spans": _spans_for_prompt(spans),
            "schema_json": json.dumps(schema_json, ensure_ascii=False),
            "rule_hints": rule_hints,
        },
    )
    data, resp = adapter.complete_json(system=system, user=user, purpose="kie")

    result = KieResult(response=resp)
    for item in data.get("fields", []) or []:
        name = item.get("name")
        if not name:
            continue
        valid_ids = _valid_span_ids(item.get("span_ids"), span_map)
        quote = " ".join(span_map[i].text for i in valid_ids) if valid_ids else None
        result.fields.append(
            ExtractedField(
                name=name,
                label=label_map.get(name),
                value_raw=(str(item["value"]) if item.get("value") is not None else None),
                span_ids=valid_ids,
                page=item.get("page"),
                source_quote=quote,
            )
        )

    for table in data.get("tables", []) or []:
        rows: list[dict[str, TableCell]] = []
        for row in table.get("rows", []) or []:
            cells: dict[str, TableCell] = {}
            for col, cell in (row.get("cells", {}) or {}).items():
                cells[col] = TableCell(
                    value=(str(cell["value"]) if cell.get("value") is not None else None),
                    span_ids=_valid_span_ids(cell.get("span_ids"), span_map),
                )
            rows.append(cells)
        result.tables.append(TableResult(name=table.get("name", "table"), rows=rows))

    result.unmapped_required = [str(x) for x in (data.get("unmapped_required") or [])]
    return result
