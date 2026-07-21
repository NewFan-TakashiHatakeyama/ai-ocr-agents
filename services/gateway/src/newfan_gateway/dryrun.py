"""dry-run（sink プレビュー, §16 設計 v0.2 §9 / P6）。

sink を実行せず「生成される SQL / ペイロードのプレビュー」を返す。SQL は
newfan_workflow.dbsink.build_db_write_sql — **実行側（orchestrator の db_write ノード）と
同一の関数** — で作るため、「プレビュー = 実 SQL」は実装の同一性で保証される（DoD）。

db_write の列はグラフから静的に決める（直前の map_fields の出力列）。マッピングが無いと
実行時の列は抽出フィールド名に依存して事前に確定できないため、プレビュー不能＝
dry-run 失敗とする（L007 warning より強い。dry-run 成功は activate の前提条件, §8）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from newfan_workflow import WorkflowGraph
from newfan_workflow.dbsink import (
    LEDGER_COLUMN,
    DbSinkError,
    build_db_write_sql,
    check_allowed_table,
)
from newfan_workflow.models import (
    ConditionNode,
    DbWriteNode,
    MapFieldsNode,
    WebhookSinkNode,
)

from newfan_gateway.records import ConnectionRecord


@dataclass
class SinkPreview:
    node_id: str
    node_type: str
    ok: bool
    connection_id: str
    sql: Optional[str] = None
    payload: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    columns: list[str] = field(default_factory=list)


def _map_preds(graph: WorkflowGraph, sink_id: str) -> list[MapFieldsNode]:
    """sink 直前の map_fields ノード（実行側 build_workflow_app と同じ走査）。"""
    preds: set[str] = set()
    for e in graph.edges:
        if e.to == sink_id:
            preds.add(e.from_)
    for n in graph.nodes:
        if isinstance(n, ConditionNode):
            targets = {a.to for a in n.config.branches} | {n.config.else_}
            if sink_id in targets:
                preds.add(n.id)
    # 実行側（workflow_graph）は sorted(node_id) 順で mapped を merge する。
    # 列順を一致させないと「プレビュー = 実 SQL」が文字列として崩れる
    nodes = {n.id: n for n in graph.nodes}
    out: list[MapFieldsNode] = []
    for p in sorted(preds):
        pred_node = nodes.get(p)
        if isinstance(pred_node, MapFieldsNode):
            out.append(pred_node)
    return out


def _map_columns(graph: WorkflowGraph, sink_id: str) -> list[str]:
    """sink 直前の map_fields が作る列（実行時の merge 順と同一）。"""
    cols: list[str] = []
    for n in _map_preds(graph, sink_id):
        for m in n.config.mappings:
            if m.to not in cols:
                cols.append(m.to)
    return cols


def preview_sinks(
    graph: WorkflowGraph,
    get_connection: Any,  # Callable[[str], Optional[ConnectionRecord]]
) -> list[SinkPreview]:
    out: list[SinkPreview] = []
    for node in graph.nodes:
        if isinstance(node, DbWriteNode):
            out.append(_preview_db_write(graph, node, get_connection))
        elif isinstance(node, WebhookSinkNode):
            cols = _map_columns(graph, node.id)
            out.append(
                SinkPreview(
                    node_id=node.id,
                    node_type=node.type,
                    ok=True,
                    connection_id=node.config.connection_id,
                    columns=cols,
                    payload={
                        "event": "workflow.webhook",
                        "id": "{workflow_run_id}:" + node.id,
                        "data": {c: "…" for c in cols} or {"<抽出フィールド名>": "…"},
                    },
                )
            )
    return out


def _preview_db_write(
    graph: WorkflowGraph, node: DbWriteNode, get_connection: Any
) -> SinkPreview:
    cfg = node.config

    def fail(msg: str) -> SinkPreview:
        return SinkPreview(
            node_id=node.id, node_type=node.type, ok=False,
            connection_id=cfg.connection_id, error=msg,
        )

    conn: Optional[ConnectionRecord] = get_connection(cfg.connection_id)
    if conn is None:
        return fail(f"接続が見つかりません: {cfg.connection_id}")
    if conn.type != "postgres":
        return fail(f"db_write の接続は type=postgres が必要です（実際: {conn.type}）")
    if conn.status not in ("active", "tested"):
        return fail(
            f"接続が疎通未確認です（status={conn.status}）。POST /connections/{conn.id}/test を先に"
        )
    maps = _map_preds(graph, node.id)
    if not maps:
        return fail(
            "直前に transform.map_fields がありません。書込み列が事前に確定できないため"
            " dry-run できません（マッピングを挟んでください）"
        )
    if len(maps) > 1:
        # 分岐で別々の map から合流すると「実行される分岐によって列が変わる」＝
        # プレビューと実 SQL が乖離する（レビューで実証）。1 sink 1 map に制限する
        return fail(
            f"db_write の直前の map_fields は 1 つにしてください（現在 {len(maps)} 個: "
            f"{[m.id for m in maps]}）。分岐ごとに別の db_write ノードを置いてください"
        )
    columns = _map_columns(graph, node.id)
    if cfg.mode == "insert":
        columns = [*columns, LEDGER_COLUMN]
    try:
        check_allowed_table(cfg.table, conn.allowed_tables)
        sql = build_db_write_sql(cfg, columns)
    except DbSinkError as exc:
        return fail(str(exc))
    return SinkPreview(
        node_id=node.id, node_type=node.type, ok=True,
        connection_id=cfg.connection_id, sql=sql, columns=columns,
    )
