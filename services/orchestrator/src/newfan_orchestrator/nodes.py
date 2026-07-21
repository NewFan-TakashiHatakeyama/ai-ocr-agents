"""抽出グラフのノード（詳細設計 §4.3）。

各ノードは ExtractionState の部分更新（dict）を返す LangGraph 互換シグネチャ。
決定論ノード（confidence_score / confidence_gate）は実装済み。外部依存ノード
（structure_ocr / kie_extract / llm_correct / vl_fallback 等）はスタブで、接続先
サービス実装時に埋める。スタブは State を壊さず通過させる（部分失敗の原則, §10）。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from newfan_memory import TenantRule, apply_rule
from newfan_metrics import current_tenant, rule_auto_apply_total
from newfan_normalizers import NormContext, normalize
from newfan_schemas import ExtractedField, ExtractionState, FieldSchema, FieldType, ReviewStatus
from newfan_validators import run_validations

from newfan_orchestrator.confidence import (
    auto_elevate,
    compute_confidence,
    grounding_score,
    ocr_confidence,
)
from newfan_orchestrator.gate import Thresholds, confidence_gate

# --- 外部サービス接続ノード（スタブ） ---------------------------------------


def load_context(state: ExtractionState) -> dict[str, Any]:
    """DB から document/pages/schema/テナント設定をロード（§4.3, DD-08 検証はここ）。"""
    # TODO: gateway/DB 接続で pages・schema を投入。DD-08 は inference/validate_config と同等の
    #       言語×モデルチェックを schema 側でも行う。
    return {}


def structure_ocr(state: ExtractionState) -> dict[str, Any]:
    """structure-svc 呼出し → spans/layout/markdown 構築（§5.3, DD-02）。"""
    # TODO: newfan_paddle_client.PaddleServingClient.layout_parsing をページ並列で呼び、
    #       build_spans/build_layout_blocks で State を構築。単文字座標欠落時は ocr-svc へ crop 再問合せ。
    return {}


def quality_gate(state: ExtractionState) -> dict[str, Any]:
    """ページ品質判定 → fallback_pages（§4.3, 閾値 §2.5）。"""
    spans = state.get("spans", [])
    page_mean_thresh = 0.75
    by_page: dict[int, list[float]] = {}
    for span in spans:
        by_page.setdefault(span.page, []).append(span.conf)
    fallback = [
        page for page, confs in by_page.items() if confs and (sum(confs) / len(confs)) < page_mean_thresh
    ]
    return {"fallback_pages": sorted(fallback)}


def vl_fallback(state: ExtractionState) -> dict[str, Any]:
    """vl-svc へ品質ゲート NG ページ送付、source='vl' でマージ（§5.4, DD-09）。"""
    # TODO: vl-svc 呼出し。VL 由来 span は grounding 上限 0.7（confidence 側で強制）。
    return {}


def memory_lookup(state: ExtractionState) -> dict[str, Any]:
    """memory-svc へ kNN + アクティブルール取得（§5.8）。空でも続行（劣化運転）。"""
    return {"memory_examples": [], "active_rules": []}


def kie_extract(state: ExtractionState) -> dict[str, Any]:
    """LLM に span_ids 必須の JSON 契約で抽出（§4.6.1）。"""
    # TODO: llm-adapter 経由。JSON 不正は 1 回再試行 → failed(E3002)。
    return {}


def deterministic_normalize(state: ExtractionState) -> dict[str, Any]:
    """組込み正規化（§5.6）→ テナントルール適用（§4.3）。

    newfan_normalizers で value_normalized を確定し、grounding/confidence 判定用メタ
    （type_converted / confidence_cap / needs_review_hint / extra）を norm_meta に格納する。
    その後、memory_lookup が取得した active_rules（regex_replace/vocab_map）を適用する。
    ルール適用エラーは当該ルールを skip し WARN（§4.3）。
    """
    schema = FieldSchema.model_validate(state.get("schema", {"doc_type": "", "fields": []}))
    type_map = {f.name: f.type for f in schema.fields}
    ctx = NormContext()  # TODO: 文脈年（他日付フィールドから補完）を渡す
    active_rules = state.get("active_rules", [])

    norm_meta: dict[str, dict[str, Any]] = {}
    updated: list[ExtractedField] = []
    for field in state.get("fields", []):
        ftype = type_map.get(field.name, FieldType.STRING)
        res = normalize(ftype, field.value_raw, ctx)
        value = res.value
        if value is not None:
            value = _apply_tenant_rules(value, field.name, active_rules)
        field.value_normalized = value
        norm_meta[field.name] = {
            "type_converted": res.type_converted,
            "confidence_cap": res.confidence_cap,
            "needs_review_hint": res.needs_review_hint,
            "extra": res.extra,
        }
        updated.append(field)
    return {"fields": updated, "norm_meta": norm_meta}


def _apply_tenant_rules(value: str, field_name: str, active_rules: list[dict[str, Any]]) -> str:
    for rule_dict in active_rules:
        if rule_dict.get("field_name") not in (None, field_name):
            continue
        try:
            rule = TenantRule.model_validate(rule_dict)
        except (ValueError, KeyError):
            continue  # 不正ルールは skip（WARN 相当）
        applied = apply_rule(rule, value)
        # §12.1 rule_auto_apply_total{tenant,rule_type}。値が変わった時だけ数える。
        # 「マッチしたルール数」ではなく「実際に効いたルール数」を測る指標のため
        # （全フィールドに対してループするので、前者だと数字が意味を持たない）。
        if applied != value:
            rule_auto_apply_total.labels(
                tenant=current_tenant(), rule_type=rule.rule_type.value
            ).inc()
        value = applied
    return value


def confidence_score(state: ExtractionState) -> dict[str, Any]:
    """§5.7.2 の式で各フィールドの confidence/grounding を算出する（実装済み）。

    grounding は normalize の type_converted を加味し、date の年補完等の confidence_cap を適用する。
    """
    spans_by_id = {s.span_id: s for s in state.get("spans", [])}
    norm_meta = state.get("norm_meta", {})
    updated: list[ExtractedField] = []
    for field in state.get("fields", []):
        first = spans_by_id.get(field.span_ids[0]) if field.span_ids else None
        line_conf = first.conf if first else 0.0
        char_confs = first.char_confs if first else None
        meta = norm_meta.get(field.name, {})
        type_converted = bool(meta.get("type_converted", False))

        oc = ocr_confidence(line_conf, char_confs)
        if first is not None:
            gk = grounding_score(
                field.value_normalized,
                field.source_quote,
                source=first.source,
                type_converted=type_converted,
            )
        else:
            gk = grounding_score(
                field.value_normalized, field.source_quote, type_converted=type_converted
            )
        field.grounding_score = gk

        conf = compute_confidence(oc, gk)
        cap = meta.get("confidence_cap")
        if cap is not None:
            conf = min(conf, float(cap))
        field.confidence = conf
        updated.append(field)
    return {"fields": updated}


def llm_correct(state: ExtractionState) -> dict[str, Any]:
    """低確信フィールドのみ LLM 補正（§4.6.2, 並列8, DD-10）。"""
    # TODO: 混同文字表 + few-shot 注入。DD-10 適合時のみ自動置換。
    return {}


def validate(state: ExtractionState) -> dict[str, Any]:
    """決定論バリデーション（§5.7.3 V-*）。合格フィールドは auto-elevation（実装済み）。

    V-DUP のクロスドキュメント検知は dup_lookup 未接続のため本ノードでは呼ばない
    （memory/DB 接続後に注入）。
    """
    fields = state.get("fields", [])
    tables = state.get("tables", [])
    results = run_validations(fields, tables, today=date.today())

    by_field: dict[str, list[dict[str, Any]]] = {}
    elevate: set[str] = set()
    for r in results:
        for name in r.fields:
            by_field.setdefault(name, []).append(
                {"check": r.check_id, "passed": r.passed, "severity": r.severity.value}
            )
            if r.passed and r.elevates:
                elevate.add(name)

    for field in fields:
        checks = by_field.get(field.name)
        if checks is not None:
            field.validation = {
                "checks": checks,
                "passed": all(c["passed"] for c in checks),
            }
        if field.name in elevate:
            field.confidence = auto_elevate(field.confidence)
    return {"fields": fields}


def confidence_gate_node(state: ExtractionState) -> dict[str, Any]:
    """閾値表と always_review で review_items を生成する（実装済み, §2.5）。"""
    schema = FieldSchema.model_validate(state.get("schema", {"doc_type": "", "fields": []}))
    always = set(state.get("schema", {}).get("always_review_fields", []) or [])
    items = confidence_gate(
        state.get("fields", []),
        schema,
        thresholds=Thresholds(),
        always_review_fields=always,
    )
    return {"review_items": items}


def hitl_review(state: ExtractionState) -> dict[str, Any]:
    """interrupt() でチェックポイント保存し停止し、resume 時に human_feedback を受ける（§4.4）。

    UI が必要とする最小情報（review_items）のみを interrupt ペイロードに載せる。
    checkpointer 付きで実行された場合のみ意味を持つ（langgraph は遅延 import）。
    """
    from langgraph.types import interrupt  # 遅延 import（graph 実行時のみ必要）

    feedback = interrupt(
        {
            "type": "review_request",
            "run_id": state.get("run_id", ""),
            "items": [ri.model_dump() for ri in state.get("review_items", [])],
        }
    )
    return {"human_feedback": feedback}


def apply_feedback(state: ExtractionState) -> dict[str, Any]:
    """human_feedback（resume 値）を fields へマージし review_status を更新する（§4.3）。"""
    feedback = state.get("human_feedback") or {}
    corrections = {
        c["field_name"]: c for c in (feedback.get("corrections", []) or []) if "field_name" in c
    }
    updated: list[ExtractedField] = []
    for field in state.get("fields", []):
        corr = corrections.get(field.name)
        if corr is not None:
            field.value_normalized = corr.get("corrected_value", field.value_normalized)
            field.review_status = ReviewStatus.CORRECTED
        elif field.review_status is ReviewStatus.PENDING:
            field.review_status = ReviewStatus.APPROVED
        updated.append(field)
    return {"fields": updated}


def learn(state: ExtractionState) -> dict[str, Any]:
    """memory-svc へ登録、ルール抽出トリガ判定（§5.8, 失敗は WARN で継続）。"""
    return {}


def finalize(state: ExtractionState) -> dict[str, Any]:
    """確定データ保存、status 遷移、export enqueue（§4.3）。"""
    return {}


# --- 条件分岐（§4.1 G1/G2） --------------------------------------------------


def route_quality_gate(state: ExtractionState) -> str:
    """G1: fallback_pages があれば vl_fallback、無ければ memory_lookup。"""
    return "vl_fallback" if state.get("fallback_pages") else "memory_lookup"


def route_confidence_gate(state: ExtractionState) -> str:
    """G2: review_items があれば hitl_review、無ければ finalize。"""
    return "hitl_review" if state.get("review_items") else "finalize"
