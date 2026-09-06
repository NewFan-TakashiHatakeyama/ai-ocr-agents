"""LLM 接続ノード（§4.3 kie_extract / llm_correct）を llm-adapter で実体化する。

build_graph に adapter/bundle を渡すと、スタブの代わりにこれらのノードが使われる。
DD-10 の適用制約は llm-adapter の llm_correct 側で強制済み。
"""

from __future__ import annotations

import os
from typing import Any, Callable, Optional

from newfan_llm_adapter import LLMAdapter, PromptBundle, kie_extract, llm_correct
from newfan_metrics import correction_reuse_hits_total, current_tenant
from newfan_schemas import ExtractedField, ExtractionState, FieldSchema, ReviewStatus, Span

from newfan_orchestrator.confidence import apply_correction_confidence

NodeFn = Callable[[ExtractionState], dict[str, Any]]


def _rule_hints(active_rules: list[dict[str, Any]]) -> str:
    hints = [r.get("rule_json", {}).get("hint_text", "") for r in active_rules]
    return "\n".join(h for h in hints if h)


def region_hints_enabled() -> bool:
    """読取領域を KIE プロンプトのヒントとして渡すか（Phase 4・既定 off）。

    設計の約束は「fixture ベースの精度計測で改善が確認できた場合のみ出荷」。
    実装は入れておき、**計測で改善を示せるまで既定 off** にする。
    """
    return os.environ.get("REGION_KIE_HINTS", "").lower() in ("1", "true", "yes", "on")


def _region_px(region: dict[str, Any], pages: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """正規化 rect を当該ページの画素矩形へ射影する（設計 §5.6-1）。

    - ``"last"`` は run の総ページ数へ解決する
    - **存在しないページを指す region はヒントごと落とす**。1 ページ目へ縮退させると
      まったく違う場所を指すヒントになり、誤誘導になる
    - 寸法が無いページも落とす（射影できない）
    """
    page_count = len(pages)
    if page_count == 0:
        return None
    spec = region.get("page")
    if spec == "last":
        page_no = page_count
    elif isinstance(spec, int) and not isinstance(spec, bool):
        if not (1 <= spec <= page_count):
            return None
        page_no = spec
    else:
        return None  # include は page 必須（None は除外領域のみ）
    dim = next((p for p in pages if int(p.get("page_no", 0)) == page_no), None)
    if dim is None:
        return None
    w, h = dim.get("width"), dim.get("height")
    if not w or not h or w <= 0 or h <= 0:
        return None
    rect = region.get("rect")
    if not rect or len(rect) < 4:
        return None
    return {
        "page": page_no,
        "bbox": [
            int(round(float(rect[0]) * w)),
            int(round(float(rect[1]) * h)),
            int(round(float(rect[2]) * w)),
            int(round(float(rect[3]) * h)),
        ],
    }


def _schema_for_prompt(
    schema: dict[str, Any], pages: Optional[list[dict[str, Any]]] = None
) -> dict[str, Any]:
    """kie プロンプトへ渡す schema を作る（設計 §5.6 / §4.7）。

    kie.py は schema をそのまま ``json.dumps`` してプロンプトへ埋める。保存されている
    ``region`` は**正規化座標**なので、素通しすると LLM に「0.30」等の意味不明な数値が
    渡り、しかも領域を使っていないスキーマでも gateway が ``"region": null`` を書く
    だけでプロンプトが変わる（＝抽出結果が変わり得る）。よって ``region`` キーは
    **必ず落とす**。

    ヒントを有効化しているとき（Phase 4）に限り、代わりに当該ページの寸法で画素へ
    射影した ``region_px`` を載せる。画素にするのは、同じプロンプトに載る span の
    bbox と同じ座標系にして LLM が照合できるようにするため。

    **領域を持たない field には何も足さない**ので、領域を使っていないスキーマの
    プロンプトはヒント有効化後も現行と 1 バイトも変わらない。

    state の schema は**変更しない**（LangGraph の state は他ノードと共有され、
    checkpoint にも載る。破壊的に書き換えると再開時の入力が変わる）。
    """
    fields = schema.get("fields")
    if not isinstance(fields, list):
        return dict(schema)
    hint = region_hints_enabled() and bool(pages)
    out = dict(schema)
    new_fields = []
    for f in fields:
        if not isinstance(f, dict) or "region" not in f:
            new_fields.append(f)
            continue
        stripped = {k: v for k, v in f.items() if k != "region"}
        region = f.get("region")
        # 明細（表）にはヒントを入れない。行数が増えたり次ページへ続いたりした
        # 帳票で「領域に近い行だけ」を選ばせると、行が静かに切り捨てられる。
        # 位置ガードも TableResult を見ないので、この壊れ方はどこにも掛からない。
        if hint and isinstance(region, dict) and not f.get("columns"):
            px = _region_px(region, pages or [])
            if px is not None:
                stripped["region_px"] = px
        new_fields.append(stripped)
    out["fields"] = new_fields
    return out


def make_kie_extract(adapter: LLMAdapter, bundle: PromptBundle) -> NodeFn:
    def _node(state: ExtractionState) -> dict[str, Any]:
        result = kie_extract(
            adapter,
            bundle,
            spans=state.get("spans", []),
            layout_markdown=state.get("layout_markdown", ""),
            schema_json=_schema_for_prompt(
                dict(state.get("schema", {})), list(state.get("pages", []) or [])
            ),
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
