"""構成 lint（docs/design/16-agent-workflow.md §5）。

保存時・有効化時に実行し、UI に指摘一覧を返す。純関数。
L009/L010 は DB 参照が要るため「参照解決関数」を注入で受ける（未注入なら skip）。

規則 ID は UI との契約。番号を変えると画面の指摘が壊れる。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Literal, Mapping, Optional

from newfan_workflow.models import (
    ConditionNode,
    DbWriteNode,
    ExtractNode,
    FileSinkNode,
    HitlGateNode,
    MapFieldsNode,
    WorkflowGraph,
    is_sink,
    is_trigger,
)

Severity = Literal["error", "warning"]


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: Severity
    message: str
    node_id: Optional[str] = None


ExistsFn = Callable[[str], bool]


def lint(
    graph: WorkflowGraph,
    *,
    auto_confirm: bool = False,
    schema_exists: Optional[ExistsFn] = None,
    connection_ok: Optional[ExistsFn] = None,
) -> list[Finding]:
    """L001〜L010 を評価して指摘を返す。error が 1 つでもあれば有効化不可。

    - auto_confirm は workflows.auto_confirm（graph_json の外にある属性）
    - schema_exists / connection_ok が None の規則（L009/L010）は skip される
    """
    findings: list[Finding] = []
    adj = _adjacency(graph)
    ids = graph.node_ids()

    # L006: 参照の解決（先に見る。以降の走査は解決できる辺だけを使う）
    for e in graph.edges:
        for ref in (e.from_, e.to):
            if ref not in ids:
                findings.append(
                    Finding("L006", "error", f"edge が存在しないノードを指しています: {ref!r}")
                )
    for node in graph.nodes:
        if isinstance(node, ConditionNode):
            targets = [a.to for a in node.config.branches] + [node.config.else_]
            for ref in targets:
                if ref not in ids:
                    findings.append(
                        Finding(
                            "L006",
                            "error",
                            f"分岐の行き先が存在しません: {ref!r}",
                            node_id=node.id,
                        )
                    )

    # L001: DAG（閉路なし）
    cycle = _find_cycle(adj, ids)
    if cycle:
        findings.append(Finding("L001", "error", f"閉路があります: {' → '.join(cycle)}"))

    # L002: トリガー ≥ 1
    triggers = [n for n in graph.nodes if is_trigger(n)]
    if not triggers:
        findings.append(Finding("L002", "error", "トリガーノードがありません"))

    # L003: 到達不能ノード（トリガーが無い場合は L002 が出るので重ねない）
    if triggers and not cycle:
        reachable = _reachable(adj, [t.id for t in triggers])
        for node in graph.nodes:
            if node.id not in reachable:
                findings.append(
                    Finding(
                        "L003",
                        "error",
                        f"どのトリガーからも到達できません: {node.id}",
                        node_id=node.id,
                    )
                )

    rev = _reverse(adj, ids)

    # L004: DB・ファイル出力の上流に必ず AI-OCR 抽出
    for node in graph.nodes:
        if isinstance(node, (DbWriteNode, FileSinkNode)):
            ancestors = _reachable(rev, [node.id]) - {node.id}
            if not any(isinstance(graph.get(a), ExtractNode) for a in ancestors):
                findings.append(
                    Finding(
                        "L004",
                        "error",
                        f"{node.type} の上流に AI-OCR 抽出（process.extract）がありません",
                        node_id=node.id,
                    )
                )

    # L005: 条件分岐の else 必須（モデルでも強制。バイパス構築への防御として残す）
    for node in graph.nodes:
        if isinstance(node, ConditionNode) and (not node.config.else_ or not node.config.branches):
            findings.append(
                Finding("L005", "error", "条件分岐に else がありません", node_id=node.id)
            )

    # L007: sink 直前のマッピング未定義列（warning）
    for node in graph.nodes:
        if not isinstance(node, DbWriteNode):
            continue
        preds = [graph.get(p) for p in rev.get(node.id, set())]
        mappings = [p for p in preds if isinstance(p, MapFieldsNode)]
        if not mappings:
            findings.append(
                Finding(
                    "L007",
                    "warning",
                    "DB 格納の直前にフィールドマッピングがありません（抽出フィールド名がそのまま列名になります）",
                    node_id=node.id,
                )
            )
        elif node.config.mode == "upsert":
            produced = {m.to for mp in mappings for m in mp.config.mappings}
            missing = [k for k in node.config.keys if k not in produced]
            if missing:
                findings.append(
                    Finding(
                        "L007",
                        "warning",
                        f"upsert のキー列がマッピングで作られていません: {missing}",
                        node_id=node.id,
                    )
                )

    # L008: auto_confirm=false のまま HITL を経ずに sink へ到達する経路（warning）。
    # run.status を見る条件分岐は「needs_review を別経路へ流す」意図とみなし、
    # それを通過する経路には出さない（設計書 §16.1 の代表例が偽陽性になるため）。
    if not auto_confirm and not cycle:
        guarded = {
            n.id
            for n in graph.nodes
            if isinstance(n, HitlGateNode)
            or (
                isinstance(n, ConditionNode)
                and any(a.when.lstrip().startswith("run.status") for a in n.config.branches)
            )
        }
        pruned = {u: {v for v in vs if v not in guarded} for u, vs in adj.items() if u not in guarded}
        extracts = [n.id for n in graph.nodes if isinstance(n, ExtractNode)]
        if extracts:
            unguarded = _reachable(pruned, extracts)
            for node in graph.nodes:
                if is_sink(node) and node.id in unguarded:
                    findings.append(
                        Finding(
                            "L008",
                            "warning",
                            "HITL ゲートを経ずに出力へ到達する経路があります"
                            "（auto_confirm=false のため needs_review 帳票が滞留します）",
                            node_id=node.id,
                        )
                    )

    # L009: schema_id の実在（tenant 内）
    if schema_exists is not None:
        for node in graph.nodes:
            if isinstance(node, ExtractNode) and not schema_exists(node.config.schema_id):
                findings.append(
                    Finding(
                        "L009",
                        "error",
                        f"スキーマが存在しません: {node.config.schema_id!r}",
                        node_id=node.id,
                    )
                )

    # L010: connection の実在＋疎通済み
    if connection_ok is not None:
        for node in graph.nodes:
            conn = getattr(node.config, "connection_id", None)
            if isinstance(conn, str) and not connection_ok(conn):
                findings.append(
                    Finding(
                        "L010",
                        "error",
                        f"接続が存在しないか疎通テスト未実施です: {conn!r}",
                        node_id=node.id,
                    )
                )

    return sorted(findings, key=lambda f: (f.severity != "error", f.rule, f.node_id or ""))


def has_errors(findings: Iterable[Finding]) -> bool:
    return any(f.severity == "error" for f in findings)


# ---------- グラフ走査 ----------
# 実行は condition ノードでは branches/else に従い、他は edges に従う。
# lint の走査は両方の和集合を使う（分岐設定でしか到達できないノードも「到達可能」）。


def _adjacency(graph: WorkflowGraph) -> dict[str, set[str]]:
    ids = graph.node_ids()
    adj: dict[str, set[str]] = {i: set() for i in ids}
    for e in graph.edges:
        if e.from_ in ids and e.to in ids:
            adj[e.from_].add(e.to)
    for node in graph.nodes:
        if isinstance(node, ConditionNode):
            for t in [a.to for a in node.config.branches] + [node.config.else_]:
                if t in ids:
                    adj[node.id].add(t)
    return adj


def _reverse(adj: Mapping[str, set[str]], ids: set[str]) -> dict[str, set[str]]:
    rev: dict[str, set[str]] = {i: set() for i in ids}
    for u, vs in adj.items():
        for v in vs:
            rev[v].add(u)
    return rev


def _reachable(adj: Mapping[str, set[str]], roots: list[str]) -> set[str]:
    seen: set[str] = set()
    stack = list(roots)
    while stack:
        u = stack.pop()
        if u in seen:
            continue
        seen.add(u)
        stack.extend(adj.get(u, set()) - seen)
    return seen


def _find_cycle(adj: Mapping[str, set[str]], ids: set[str]) -> Optional[list[str]]:
    """閉路を 1 つ返す（無ければ None）。UI に「どこが回っているか」を見せるため経路を返す。"""
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = dict.fromkeys(ids, WHITE)
    stack: list[str] = []

    def dfs(u: str) -> Optional[list[str]]:
        color[u] = GRAY
        stack.append(u)
        for v in sorted(adj.get(u, set())):
            if color.get(v, WHITE) == WHITE:
                found = dfs(v)
                if found:
                    return found
            elif color.get(v) == GRAY:
                return stack[stack.index(v) :] + [v]
        color[u] = BLACK
        stack.pop()
        return None

    for nid in sorted(ids):
        if color[nid] == WHITE:
            found = dfs(nid)
            if found:
                return found
    return None


__all__ = ["Finding", "Severity", "lint", "has_errors"]
