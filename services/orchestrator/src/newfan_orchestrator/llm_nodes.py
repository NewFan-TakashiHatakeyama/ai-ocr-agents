"""LLM 接続ノード（§4.3 kie_extract / llm_correct）を llm-adapter で実体化する。

build_graph に adapter/bundle を渡すと、スタブの代わりにこれらのノードが使われる。
DD-10 の適用制約は llm-adapter の llm_correct 側で強制済み。
"""

from __future__ import annotations

from typing import Any, Callable

from newfan_llm_adapter import LLMAdapter, PromptBundle, kie_extract, llm_correct
from newfan_metrics import correction_reuse_hits_total, current_tenant
from newfan_schemas import ExtractedField, ExtractionState, FieldSchema, ReviewStatus, Span

from newfan_orchestrator.confidence import apply_correction_confidence

NodeFn = Callable[[ExtractionState], dict[str, Any]]


def _rule_hints(active_rules: list[dict[str, Any]]) -> str:
    hints = [r.get("rule_json", {}).get("hint_text", "") for r in active_rules]
    return "\n".join(h for h in hints if h)


def _schema_for_prompt(schema: dict[str, Any]) -> dict[str, Any]:
    """kie プロンプトへ渡す schema から ``region`` キーを落とす（設計 §5.6 / §4.7）。

    kie.py は schema をそのまま ``json.dumps`` してプロンプトへ埋める。region は
    **正規化座標**なので、素通しすると LLM に「0.30」等の意味不明な数値が渡り、
    しかも領域を使っていないスキーマでも gateway が ``"region": null`` を書くだけで
    プロンプトが変わる（＝抽出結果が変わり得る）。この関数がその 1 バイトの差も
    含めて現行と同一にする役目を負う。

    プロンプトへの領域ヒント注入は Phase 4（画素へ射影した ``region_px`` を渡す形）
    で改めて設計・計測する。ここではキーを落とすだけ。

    state の schema は**変更しない**（LangGraph の state は他ノードと共有され、
    checkpoint にも載る。破壊的に書き換えると再開時の入力が変わる）。
    """
    fields = schema.get("fields")
    if not isinstance(fields, list):
        return dict(schema)
    out = dict(schema)
    out["fields"] = [
        {k: v for k, v in f.items() if k != "region"}
        if isinstance(f, dict) and "region" in f
        else f
        for f in fields
    ]
    return out


def make_kie_extract(adapter: LLMAdapter, bundle: PromptBundle) -> NodeFn:
    def _node(state: ExtractionState) -> dict[str, Any]:
        result = kie_extract(
            adapter,
            bundle,
            spans=state.get("spans", []),
            layout_markdown=state.get("layout_markdown", ""),
            schema_json=_schema_for_prompt(dict(state.get("schema", {}))),
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
                # §12.1 correction_reuse_hits_total（価値 KPI）。
                # memory_refs が付いている＝過去の修正メモリを引いて直せた補正。
                # 「学習が実際に効いているか」を測る唯一の指標なので、適用が確定した
                # ここで数える（参照しただけ・採用されなかった補正は数えない）。
                if result.memory_refs:
                    correction_reuse_hits_total.labels(tenant=current_tenant()).inc()
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
