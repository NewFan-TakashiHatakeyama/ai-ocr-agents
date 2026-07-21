# mypy: ignore-errors
"""graph_json → LangGraph StateGraph の動的構築（§16 設計 v0.2 §6.1〜6.2）。

構築+compile は実測 median 0.82ms（scripts/probe_langgraph_workflow.py）で、
実行時構築に性能上の問題は無い。runner 側で (workflow_id, version) キーの
キャッシュを持つ（版は固定なので再検証不要）。

ノード実装の冪等規則（§6.4、実測した再実行境界に基づく）:
- 完了済みノードは resume で再実行されない
- interrupt を含むノードの interrupt より前のコードは resume のたびに再実行される
  → process.extract の enqueue は idem_key で 1 回に抑える（workflow_store）
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Any, Callable, Optional, TypedDict

from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from newfan_metrics import sink_write_rows_total, workflow_node_duration_seconds
from newfan_workflow import EvalContext, FieldView, WorkflowGraph, evaluate, is_trigger, parse_expr
from newfan_workflow.dbsink import (
    LEDGER_COLUMN,
    build_db_write_sql,
    build_dsn,
    check_allowed_table,
    check_row_limit,
)
from newfan_workflow.models import (
    ClassifyNode,
    ConditionNode,
    DbWriteNode,
    ExtractNode,
    FileSinkNode,
    HitlGateNode,
    ManualNode,
    MapFieldsNode,
    NotifyNode,
    WebhookSinkNode,
)

from newfan_orchestrator.workflow_store import WorkflowRunStore

logger = logging.getLogger(__name__)

WORKFLOW_STREAM = "q.workflow"

# 割り込みの分類（§6.2）。runner が workflow_runs.status への写像に使う契約
KIND_AWAIT_EXTRACT = "await_extract"
KIND_AWAIT_HITL = "await_hitl"

SendWebhookFn = Callable[[str, str, dict[str, Any]], bool]


def _merge_dicts(a: Optional[dict], b: Optional[dict]) -> dict:
    """node_outputs の reducer。既定の last-value チャネルだと後続ノードの返り値が
    先行ノードの出力を丸ごと上書きし、webhook が map の結果を読めなくなる。"""
    return {**(a or {}), **(b or {})}


class WorkflowState(TypedDict, total=False):
    workflow_run_id: str
    tenant_id: str
    document_id: str
    run_id: str
    run_status: str
    doc_type: Optional[str]
    run_confidence: Optional[float]
    fields: dict[str, Any]  # field名 → {"value": str|None, "confidence": float}
    node_outputs: Annotated[dict[str, Any], _merge_dicts]


WriteDbFn = Callable[[str, str, list[dict[str, Any]]], int]  # (dsn, sql, rows) -> 行数
ResolveSecretFn = Callable[[str], str]  # secret_ref -> 秘密（Secrets Manager 解決）
WriteFileFn = Callable[[str, str, bytes, str], None]  # (bucket, key, body, content_type)
SendNotifyFn = Callable[[str, str], bool]  # (url, text) -> 成否


@dataclass
class RunnerDeps:
    store: WorkflowRunStore
    send_webhook: SendWebhookFn
    # P6: sink.db_write。未配線のグラフで db_write ノードに達したら明示的に落とす
    write_db: Optional[WriteDbFn] = None
    resolve_secret: Optional[ResolveSecretFn] = None
    # P8: sink.file / sink.notify
    write_file: Optional[WriteFileFn] = None
    send_notify: Optional[SendNotifyFn] = None


def _wrap(node_id: str, node_type: str, fn: Callable, store: WorkflowRunStore) -> Callable:
    """ノード実行を workflow_node_runs へ記録するラッパ（§3.1 観測用の射影）。"""

    def wrapped(state: WorkflowState) -> dict[str, Any]:
        tenant = state["tenant_id"]
        run_id = state["workflow_run_id"]
        store.node_run_start(tenant, run_id, node_id, node_type)
        t0 = time.monotonic()
        try:
            out = fn(state)
        except GraphInterrupt:
            # 待機は失敗ではない。node_run は running のまま（resume 後に確定する）
            raise
        except Exception as exc:  # noqa: BLE001 - 失敗を射影へ記録してから伝播
            store.node_run_finish(
                tenant, run_id, node_id, "failed", error=str(exc)[:2000]
            )
            raise
        workflow_node_duration_seconds.labels(type=node_type).observe(time.monotonic() - t0)
        store.node_run_finish(
            tenant, run_id, node_id, "succeeded", output=(out or {}).get("node_outputs", {}).get(node_id)
        )
        return out

    return wrapped


def _make_extract(node: ExtractNode, deps: RunnerDeps) -> Callable:
    def run(state: WorkflowState) -> dict[str, Any]:
        idem = f"{state['workflow_run_id']}:{node.id}"
        run_id = deps.store.ensure_extract_run(
            state["tenant_id"],
            state["document_id"],
            node.config.schema_id,
            node.config.options.model_dump(),
            idem,
            notify={"stream": WORKFLOW_STREAM, "workflow_run_id": state["workflow_run_id"]},
        )
        # ここで実行が checkpoint に固定され、抽出完了 notify で resume される。
        # event は runner が load_extract_result で enrich 済み（fields/doc_type/confidence）。
        event = interrupt({"kind": KIND_AWAIT_EXTRACT, "run_id": run_id, "node_id": node.id})
        return {
            "run_id": event.get("run_id", run_id),
            "run_status": event.get("status", ""),
            "doc_type": event.get("doc_type"),
            "run_confidence": event.get("run_confidence"),
            "fields": event.get("fields", {}),
            "node_outputs": {node.id: {"run_id": run_id, "status": event.get("status")}},
        }

    return run


def _make_map_fields(node: MapFieldsNode) -> Callable:
    def run(state: WorkflowState) -> dict[str, Any]:
        fields = state.get("fields") or {}
        out: dict[str, Any] = {}
        for m in node.config.mappings:
            if m.const is not None:
                value: Any = m.const
            else:
                value = (fields.get(m.from_ or "") or {}).get("value")
            if m.mask and value is not None:
                value = "***"  # §16.2 マスキング（P6 で書式と併せて拡充）
            out[m.to] = value
        return {"node_outputs": {node.id: {"mapped": out}}}

    return run


def _make_webhook(
    node: WebhookSinkNode, deps: RunnerDeps, map_preds: list[str]
) -> Callable:
    def run(state: WorkflowState) -> dict[str, Any]:
        conn = deps.store.get_webhook_connection(state["tenant_id"], node.config.connection_id)
        if conn is None:
            # activate 時の L010 で防いでいるが、後から接続が消された場合はここで止まる
            raise RuntimeError(
                f"webhook 接続が見つからないか疎通未確認です: {node.config.connection_id}"
            )
        url, secret = conn

        data = _sink_data(state, map_preds)
        event = {
            "event": "workflow.webhook",
            # 受信側の dedupe 用（at-least-once 配信, §6.4）
            "id": f"{state['workflow_run_id']}:{node.id}",
            "tenant_id": state["tenant_id"],
            "workflow_run_id": state["workflow_run_id"],
            "document_id": state.get("document_id"),
            "run_id": state.get("run_id"),
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "data": data,
        }
        if not deps.send_webhook(url, secret, event):
            raise RuntimeError(f"webhook 配信に失敗しました: node={node.id}")
        return {"node_outputs": {node.id: {"delivered": True, "url": url}}}

    return run


def _make_router(node: ConditionNode) -> Callable:
    arms = [(parse_expr(a.when), a.to) for a in node.config.branches]
    default = node.config.else_

    def route(state: WorkflowState) -> str:
        ctx = EvalContext(
            run_status=state.get("run_status") or None,
            run_confidence=state.get("run_confidence"),
            doc_type=state.get("doc_type"),
            fields={
                name: FieldView(value=f.get("value"), confidence=f.get("confidence"))
                for name, f in (state.get("fields") or {}).items()
            },
        )
        for cond, to in arms:
            if evaluate(cond, ctx):
                return to
        return default

    return route


def _sink_data(state: WorkflowState, map_preds: list[str]) -> dict[str, Any]:
    """sink の入力行。直前の map_fields の出力を優先し、無ければ fields の生値。"""
    outputs = state.get("node_outputs") or {}
    mapped: dict[str, Any] = {}
    for p in map_preds:
        mapped.update((outputs.get(p) or {}).get("mapped") or {})
    return mapped or {k: v.get("value") for k, v in (state.get("fields") or {}).items()}


def _make_db_write(node: DbWriteNode, deps: RunnerDeps, map_preds: list[str]) -> Callable:
    """sink.db_write（DD-12 / P6）。

    安全策の順序（§9）: allowed_tables 完全一致 → 識別子検証つき SQL 生成
    （dry-run と同一の dbsink.build_db_write_sql）→ プリペアド実行 → 行数上限。
    冪等（§6.4）: upsert は自然冪等。insert は台帳キー nf_write_key を付与し
    ON CONFLICT DO NOTHING（対象テーブルに UNIQUE があれば正確に 1 回）。
    """

    def run(state: WorkflowState) -> dict[str, Any]:
        cfg = node.config
        try:
            if deps.write_db is None:
                raise RuntimeError("db_write が配線されていません（RunnerDeps.write_db）")
            info = deps.store.get_db_connection(state["tenant_id"], cfg.connection_id)
            if info is None:
                raise RuntimeError(
                    f"postgres 接続が見つからないか疎通未確認です: {cfg.connection_id}"
                )
            check_allowed_table(cfg.table, info.allowed_tables)

            # webhook と違い fields の生値へは fallback **しない**。fallback すると
            # dry-run で一度もプレビューされていない列（抽出フィールド名・mask 迂回）が
            # 顧客 DB に書かれ得る（レビューで検出）。map_fields の出力だけが書込み対象
            outputs = state.get("node_outputs") or {}
            row: dict[str, Any] = {}
            for pred in map_preds:
                row.update((outputs.get(pred) or {}).get("mapped") or {})
            if not row:
                raise RuntimeError(
                    "直前の map_fields の出力がありません（分岐が map_fields を迂回して"
                    " db_write に到達した可能性。dry-run 済みの列以外は書きません）"
                )
            if cfg.mode == "insert":
                # 書込み台帳キー（workflow_run_id:node_id:行番号）。§6.4
                row[LEDGER_COLUMN] = f"{state['workflow_run_id']}:{node.id}:0"
            rows = [row]
            check_row_limit(len(rows))
            sql = build_db_write_sql(cfg, list(row.keys()))

            secret = None
            if info.secret_ref:
                if deps.resolve_secret is None:
                    raise RuntimeError("secret_ref があるのに resolver が配線されていません")
                secret = deps.resolve_secret(info.secret_ref)
            dsn = build_dsn(info.config, secret)

            written = deps.write_db(dsn, sql, rows)
        except GraphInterrupt:  # pragma: no cover - db_write は interrupt しない
            raise
        except Exception:
            if cfg.on_failure == "skip_and_notify":
                # 下流を止めない選択（§9 on_failure）。通知は §12 の監視（P8）が拾えるよう
                # 警告ログ + 射影に skipped を残す
                logger.warning(
                    "[workflow] db_write 失敗を skip（on_failure=skip_and_notify）: node=%s",
                    node.id, exc_info=True,
                )
                return {"node_outputs": {node.id: {"skipped": True}}}
            # retry / halt_notify は失敗として伝播（retry は §11.2 の retry API で再実行）
            raise
        sink_write_rows_total.labels(connection=cfg.connection_id).inc(written)
        return {
            "node_outputs": {
                node.id: {"written": written, "table": cfg.table, "mode": cfg.mode}
            }
        }

    return run


def _make_classify(node: ClassifyNode) -> Callable:
    """process.classify（P8）。抽出結果の doc_type を許可リストと照合する。

    分類そのものは抽出グラフ（§4）が行い doc_type を返す（DD-11: ここで再分類しない）。
    on_unknown=halt は対象外帳票で run を止める。default_route は素通り
    （下流の branch.condition が doc.doc_type で経路を分ける前提）。
    """

    def run(state: WorkflowState) -> dict[str, Any]:
        dt = state.get("doc_type")
        known = dt in node.config.doc_types
        if not known and node.config.on_unknown == "halt":
            raise RuntimeError(
                f"doc_type が分類対象外です: {dt!r}（対象: {node.config.doc_types}）"
            )
        return {"node_outputs": {node.id: {"doc_type": dt, "known": known}}}

    return run


def _render_path(template: str, state: WorkflowState, node_id: str) -> str:
    """sink.file のパス。プレースホルダのみ許可（任意式は入れない）。"""
    values = {
        "workflow_run_id": state.get("workflow_run_id", ""),
        "document_id": state.get("document_id", ""),
        "node_id": node_id,
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }
    out = template
    for k, v in values.items():
        out = out.replace("{" + k + "}", str(v))
    return out.lstrip("/")


def _make_file(node: FileSinkNode, deps: RunnerDeps, map_preds: list[str]) -> Callable:
    """sink.file（P8）。canonical な断面を S3（type='s3' 接続のバケット）へ書く。

    冪等: キーは run 単位で決定的（既定でパスに workflow_run_id を含める運用）。
    再実行は同一キーへの上書きで無害（§6.4）。
    """

    def run(state: WorkflowState) -> dict[str, Any]:
        if deps.write_file is None:
            raise RuntimeError("file sink が配線されていません（RunnerDeps.write_file）")
        bucket = deps.store.get_s3_connection(state["tenant_id"], node.config.connection_id)
        if bucket is None:
            raise RuntimeError(
                f"s3 接続が見つからないか疎通未確認です: {node.config.connection_id}"
            )
        data = _sink_data(state, map_preds)
        key = _render_path(node.config.path, state, node.id)
        if node.config.format == "csv":
            import csv
            import io as _io

            buf = _io.StringIO()
            w = csv.DictWriter(buf, fieldnames=list(data.keys()))
            w.writeheader()
            w.writerow(data)
            body, ctype = buf.getvalue().encode("utf-8-sig"), "text/csv"  # Excel 互換 BOM
        else:
            body = json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")
            ctype = "application/json"
        deps.write_file(bucket, key, body, ctype)
        return {"node_outputs": {node.id: {"bucket": bucket, "key": key, "bytes": len(body)}}}

    return run


class _SafeMap(dict):
    def __missing__(self, key: str) -> str:  # 未定義プレースホルダは空文字（通知を止めない）
        return ""


def _make_notify(node: NotifyNode, deps: RunnerDeps) -> Callable:
    """sink.notify（P8）。Slack incoming webhook（connections type='webhook'）へ
    {"text": テンプレート展開} を POST する。when 式が偽なら送らない。"""

    def run(state: WorkflowState) -> dict[str, Any]:
        if deps.send_notify is None:
            raise RuntimeError("notify が配線されていません（RunnerDeps.send_notify）")
        if node.config.when:
            ctx = EvalContext(
                run_status=state.get("run_status") or None,
                run_confidence=state.get("run_confidence"),
                doc_type=state.get("doc_type"),
                fields={
                    name: FieldView(value=f.get("value"), confidence=f.get("confidence"))
                    for name, f in (state.get("fields") or {}).items()
                },
            )
            if not evaluate(parse_expr(node.config.when), ctx):
                return {"node_outputs": {node.id: {"sent": False, "reason": "when=false"}}}
        conn = deps.store.get_webhook_connection(state["tenant_id"], node.config.connection_id)
        if conn is None:
            raise RuntimeError(
                f"webhook 接続が見つからないか疎通未確認です: {node.config.connection_id}"
            )
        url, _secret = conn  # Slack incoming webhook は URL 自体が秘密。署名は使わない
        values = _SafeMap(
            workflow_run_id=state.get("workflow_run_id", ""),
            document_id=state.get("document_id", ""),
            run_status=state.get("run_status", ""),
            doc_type=state.get("doc_type") or "",
        )
        for name, f in (state.get("fields") or {}).items():
            values[name] = f.get("value") or ""
        text = node.config.template.format_map(values)
        if not deps.send_notify(url, text):
            raise RuntimeError(f"notify 送信に失敗しました: node={node.id}")
        return {"node_outputs": {node.id: {"sent": True}}}

    return run


def _make_hitl_gate(node: HitlGateNode) -> Callable:
    """branch.hitl_gate（§8 / P5）。

    上流 extract の結果が needs_review のときだけ interrupt({kind: "await_hitl"}) して
    人の確定を待つ（runner が status='waiting_hitl' へ写像する）。confirmed 等は素通り。
    priority_boost 等の config は interrupt payload に載せ、runner が
    workflow_runs.waiting へ永続化する → gateway のレビューキューが優先度へ加点する。

    再開は confirm_done notify（worker の確定フロー完了 notify）。event は runner が
    load_extract_result で enrich 済みなので、確定後の final_value が下流へ流れる。
    純関数（interrupt 前に副作用なし）＝再実行境界に対して無条件に安全（§6.4）。
    """

    def run(state: WorkflowState) -> dict[str, Any]:
        if state.get("run_status") != "needs_review":
            return {"node_outputs": {node.id: {"status": "passed"}}}
        payload: dict[str, Any] = {
            "kind": KIND_AWAIT_HITL,
            "run_id": state.get("run_id"),
            "node_id": node.id,
        }
        cfg = node.config
        if cfg.priority_boost:
            payload["priority_boost"] = cfg.priority_boost
        if cfg.assignee_group:
            payload["assignee_group"] = cfg.assignee_group
        if cfg.sla_hours:
            payload["sla_hours"] = cfg.sla_hours
        event = interrupt(payload)
        return {
            "run_status": event.get("status") or state.get("run_status", ""),
            "doc_type": event.get("doc_type", state.get("doc_type")),
            "run_confidence": event.get("run_confidence", state.get("run_confidence")),
            "fields": event.get("fields") or state.get("fields", {}),
            "node_outputs": {node.id: {"status": event.get("status")}},
        }

    return run


def _noop(state: WorkflowState) -> dict[str, Any]:
    return {}


def build_workflow_app(
    wf: WorkflowGraph, deps: RunnerDeps, *, checkpointer: Any = None
) -> Any:
    """検証済み WorkflowGraph から実行可能な compiled graph を作る。

    実行できるのは IMPLEMENTED_NODE_TYPES（activate の capability 検査で保証）のみ。
    未知の実装が紛れ込んだ場合はここで即座に落とす（黙って素通りさせない）。
    """
    sg = StateGraph(WorkflowState)
    cond_ids = {n.id for n in wf.nodes if isinstance(n, ConditionNode)}

    # 分岐設定（branches/else）も走査対象に含めた前任ノード表（webhook の入力解決用）
    preds: dict[str, set[str]] = {n.id: set() for n in wf.nodes}
    for e in wf.edges:
        if e.to in preds and e.from_ not in cond_ids:
            preds[e.to].add(e.from_)
    for n in wf.nodes:
        if isinstance(n, ConditionNode):
            for t in [a.to for a in n.config.branches] + [n.config.else_]:
                if t in preds:
                    preds[t].add(n.id)

    for node in wf.nodes:
        if isinstance(node, ExtractNode):
            fn = _make_extract(node, deps)
        elif isinstance(node, MapFieldsNode):
            fn = _make_map_fields(node)
        elif isinstance(node, WebhookSinkNode):
            map_preds = [
                p for p in sorted(preds[node.id]) if isinstance(wf.get(p), MapFieldsNode)
            ]
            fn = _make_webhook(node, deps, map_preds)
        elif isinstance(node, DbWriteNode):
            map_preds = [
                p for p in sorted(preds[node.id]) if isinstance(wf.get(p), MapFieldsNode)
            ]
            fn = _make_db_write(node, deps, map_preds)
        elif isinstance(node, FileSinkNode):
            map_preds = [
                p for p in sorted(preds[node.id]) if isinstance(wf.get(p), MapFieldsNode)
            ]
            fn = _make_file(node, deps, map_preds)
        elif isinstance(node, NotifyNode):
            fn = _make_notify(node, deps)
        elif isinstance(node, ClassifyNode):
            fn = _make_classify(node)
        elif isinstance(node, HitlGateNode):
            fn = _make_hitl_gate(node)
        elif isinstance(node, (ConditionNode, ManualNode)) or is_trigger(node):
            fn = _noop
        else:
            raise RuntimeError(
                f"未実装のノード種別です（activate の capability 検査をすり抜けた）: {node.type}"
            )
        sg.add_node(node.id, _wrap(node.id, node.type, fn, deps.store))

    outgoing: dict[str, set[str]] = {n.id: set() for n in wf.nodes}
    for e in wf.edges:
        # 条件分岐ノードからの辺は UI 描画用の重複。実行は branches/else に従う（§4.1）
        if e.from_ in cond_ids:
            continue
        sg.add_edge(e.from_, e.to)
        outgoing[e.from_].add(e.to)
    for node in wf.nodes:
        if isinstance(node, ConditionNode):
            targets = {a.to for a in node.config.branches} | {node.config.else_}
            sg.add_conditional_edges(node.id, _make_router(node), {t: t for t in targets})
            outgoing[node.id] |= targets

    for node in wf.nodes:
        if is_trigger(node):
            sg.add_edge(START, node.id)
        if not outgoing[node.id] and not isinstance(node, ConditionNode):
            sg.add_edge(node.id, END)

    return sg.compile(checkpointer=checkpointer)
