"""LLM 接続ノード（§4.3 kie_extract / llm_correct）を llm-adapter で実体化する。

build_graph に adapter/bundle を渡すと、スタブの代わりにこれらのノードが使われる。
DD-10 の適用制約は llm-adapter の llm_correct 側で強制済み。
"""

from __future__ import annotations

from typing import Any, Callable

from newfan_llm_adapter import LLMAdapter, PromptBundle, kie_extract, llm_correct
from newfan_schemas import ExtractedField, ExtractionState, FieldSchema, ReviewStatus, Span

from newfan_orchestrator.confidence import apply_correction_confidence

NodeFn = Callable[[ExtractionState], dict[str, Any]]


def _rule_hints(active_rules: list[dict[str, Any]]) -> str:
    hints = [r.get("rule_json", {}).get("hint_text", "") for r in active_rules]
    return "\n".join(h for h in hints if h)


def make_kie_extract(adapter: LLMAdapter, bundle: PromptBundle) -> NodeFn:
    def _node(state: ExtractionState) -> dict[str, Any]:
        result = kie_extract(
            adapter,
            bundle,
            spans=state.get("spans", []),
            layout_markdown=state.get("layout_markdown", ""),
            schema_json=dict(state.get("schema", {})),
            rule_hints=_rule_hints(state.get("active_rules", [])),
        )
        # 構造由来テーブル（structure_ocr が cell 座標付きで生成）を優先し、
        # 無い場合のみ LLM 抽出のテーブルを使う（§5.3: 座標/構造が正確）。
        tables = state.get("tables") or result.tables
        return {"fields": result.fields, "tables": tables}

    return _node


def make_llm_correct(
    adapter: LLMAdapter, bundle: PromptBundle, *, low_conf_threshold: float = 0.80
) -> NodeFn:
    def _node(state: ExtractionState) -> dict[str, Any]:
        schema = FieldSchema.model_validate(state.get("schema", {"doc_type": "", "fields": []}))
        type_map = {f.name: f.type.value for f in schema.fields}
        spans_by_id: dict[int, Span] = {s.span_id: s for s in state.get("spans", [])}

        updated: list[ExtractedField] = []
        for field in state.get("fields", []):
            if field.confidence >= low_conf_threshold or not field.value_raw:
                updated.append(field)
                continue

            first = spans_by_id.get(field.span_ids[0]) if field.span_ids else None
            char_confs = first.char_confs if first else None
            result = llm_correct(
                adapter,
                bundle,
                field_name=field.name,
                field_type=type_map.get(field.name, "string"),
                fmt="",
                value_raw=field.value_raw,
                char_confs=char_confs,
                context=field.source_quote or "",
            )

            if result.applied and result.corrected is not None:
                field.correction = {
                    "applied": True,
                    "from": field.value_raw,
                    "to": result.corrected,
                    "by": "llm_correct",
                    "used_pairs": result.used_pairs,
                    "memory_refs": result.memory_refs,
                    "rationale": result.rationale,
                }
                field.value_normalized = result.corrected
                field.confidence = apply_correction_confidence(
                    field.confidence, result.confidence, dd10_ok=True
                )
            elif result.needs_review:
                field.review_status = ReviewStatus.PENDING
                field.correction = {"applied": False, "needs_review": True}
            updated.append(field)
        return {"fields": updated}

    return _node
