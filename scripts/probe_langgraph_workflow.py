"""LangGraph をワークフロー層エンジンに使えるかの実測（調査報告書の要検証 1-4）。

出力は ASCII のみ（Windows cp932 コンソールでの文字化け回避）。
  1: checkpointer schema separation (search_path)
  2: non-owner app role via PostgresSaver
  3a/3b: interrupt -> process exit -> resume in NEW process (+ replay semantics)
  4: dynamic StateGraph build from graph_json + compile() latency
"""

from __future__ import annotations

import statistics
import sys
import time
from pathlib import Path

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver

SCHEMA = "lg_wf"
OWNER_KW = "host=localhost port=5433 dbname=newfan user=newfan password=newfan"
APP_KW = "host=localhost port=5433 dbname=newfan user=newfan_app password=localpw123"


def kw_with_schema(kw: str) -> str:
    return kw + f" options='-csearch_path={SCHEMA}'"


COUNTERS = Path(__file__).with_name("probe3_counters.txt")


def read_counters() -> dict[str, int]:
    if not COUNTERS.exists():
        return {}
    return {
        k: int(v)
        for k, v in (ln.split("=") for ln in COUNTERS.read_text().splitlines() if "=" in ln)
    }


def bump(name: str) -> None:
    c = read_counters()
    c[name] = c.get(name, 0) + 1
    COUNTERS.write_text("\n".join(f"{k}={v}" for k, v in sorted(c.items())))


def probe1() -> None:
    with psycopg.connect(OWNER_KW, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    with PostgresSaver.from_conn_string(kw_with_schema(OWNER_KW)) as saver:
        saver.setup()
    with psycopg.connect(OWNER_KW) as conn:
        rows = conn.execute(
            "SELECT schemaname, tablename FROM pg_tables"
            " WHERE tablename LIKE 'checkpoint%' ORDER BY 1, 2"
        ).fetchall()
        print("[1] checkpoint tables by schema:")
        for r in rows:
            print("   ", r[0], r[1])
    with psycopg.connect(kw_with_schema(OWNER_KW)) as conn:
        sp = conn.execute("SHOW search_path").fetchone()
        print("[1] search_path via options:", sp)

    cfg = {"configurable": {"thread_id": "probe_sep", "checkpoint_ns": ""}}
    blank = {
        "v": 4, "id": "c-sep", "ts": "2026-07-21T00:00:00+00:00",
        "channel_values": {"marker": "in-lg_wf"}, "channel_versions": {},
        "versions_seen": {}, "pending_sends": [],
    }
    with PostgresSaver.from_conn_string(kw_with_schema(OWNER_KW)) as wf_saver:
        wf_saver.put(cfg, blank, {"source": "input", "step": 0}, {})
        got_wf = wf_saver.get(cfg)
    with PostgresSaver.from_conn_string(OWNER_KW) as pub_saver:  # search_path = public
        got_pub = pub_saver.get(cfg)
    print("[1] thread visible in lg_wf saver:", got_wf is not None)
    print("[1] same thread visible in public saver:", got_pub is not None)
    print("[1] SEPARATION:", "OK" if (got_wf and not got_pub) else "BROKEN")


def probe2() -> None:
    with psycopg.connect(OWNER_KW, autocommit=True) as conn:
        conn.execute(f"GRANT USAGE ON SCHEMA {SCHEMA} TO newfan_app")
        conn.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {SCHEMA} TO newfan_app")
    with psycopg.connect(kw_with_schema(APP_KW)) as conn:
        who = conn.execute(
            "SELECT current_user, rolsuper, rolbypassrls FROM pg_roles"
            " WHERE rolname = current_user"
        ).fetchone()
        print("[2] connect as:", who)
    cfg = {"configurable": {"thread_id": "probe_approle", "checkpoint_ns": ""}}
    blank = {
        "v": 4, "id": "c-app", "ts": "2026-07-21T00:00:00+00:00",
        "channel_values": {}, "channel_versions": {}, "versions_seen": {},
        "pending_sends": [],
    }
    with PostgresSaver.from_conn_string(kw_with_schema(APP_KW)) as saver:
        saver.put(cfg, blank, {"source": "input", "step": 0}, {})
        got = saver.get(cfg)
    print("[2] put/get via non-owner app role:", "OK" if got else "FAILED")


def _build_probe3_graph():  # noqa: ANN202
    from typing import TypedDict

    from langgraph.graph import END, START, StateGraph
    from langgraph.types import interrupt

    class S(TypedDict, total=False):
        doc: str
        approved: str

    def extract(state: S) -> dict:
        bump("extract_runs")
        return {"doc": "doc_probe3"}

    def hitl(state: S) -> dict:
        bump("hitl_pre_interrupt_runs")  # interrupt より前の副作用（replay 計測用）
        answer = interrupt({"kind": "await_hitl", "document_id": state.get("doc")})
        return {"approved": str(answer)}

    def sink(state: S) -> dict:
        bump("sink_runs")
        return {}

    g = StateGraph(S)
    g.add_node("extract", extract)
    g.add_node("hitl", hitl)
    g.add_node("sink", sink)
    g.add_edge(START, "extract")
    g.add_edge("extract", "hitl")
    g.add_edge("hitl", "sink")
    g.add_edge("sink", END)
    return g


def probe3_start() -> None:
    COUNTERS.unlink(missing_ok=True)
    g = _build_probe3_graph()
    cfg = {"configurable": {"thread_id": "probe3"}}
    with PostgresSaver.from_conn_string(kw_with_schema(OWNER_KW)) as saver:
        app = g.compile(checkpointer=saver)
        result = app.invoke({"doc": ""}, cfg)
    print("[3a] interrupted:", "__interrupt__" in result)
    print("[3a] counters after start:", read_counters())
    # ts を 3 日前へ書き換え（TTL が無く時刻に依存しないことの傍証）
    with psycopg.connect(kw_with_schema(OWNER_KW), autocommit=True) as conn:
        coltype = conn.execute(
            "SELECT data_type FROM information_schema.columns"
            " WHERE table_schema = %s AND table_name = 'checkpoints'"
            " AND column_name = 'checkpoint'",
            (SCHEMA,),
        ).fetchone()
        print("[3a] checkpoint column type:", coltype)
        if coltype and coltype[0] == "jsonb":
            n = conn.execute(
                "UPDATE checkpoints SET checkpoint ="
                " jsonb_set(checkpoint, '{ts}', to_jsonb('2026-07-18T00:00:00+00:00'::text))"
                " WHERE thread_id = 'probe3'"
            ).rowcount
            print("[3a] backdated ts to 3 days ago on", n, "checkpoints")


def probe3_resume() -> None:
    from langgraph.types import Command

    g = _build_probe3_graph()
    cfg = {"configurable": {"thread_id": "probe3"}}
    with PostgresSaver.from_conn_string(kw_with_schema(OWNER_KW)) as saver:
        app = g.compile(checkpointer=saver)
        result = app.invoke(Command(resume="approved-by-human"), cfg)
    print("[3b] resumed in NEW process. approved =", result.get("approved"))
    c = read_counters()
    print("[3b] counters after resume:", c)
    print("[3b] completed-node replayed:", "NO (extract=1)" if c.get("extract_runs") == 1 else f"YES ({c.get('extract_runs')})")
    print("[3b] pre-interrupt code replayed:", "YES (hitl_pre=2)" if c.get("hitl_pre_interrupt_runs") == 2 else f"={c.get('hitl_pre_interrupt_runs')}")


def probe4() -> None:
    import json

    sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
    from newfan_workflow import WorkflowGraph, evaluate, parse_expr
    from langgraph.graph import END, START, StateGraph

    graph_json = json.loads(Path(__file__).with_name("probe4_graph.json").read_text("utf-8"))

    def build(gj: dict):  # noqa: ANN202
        wf = WorkflowGraph.model_validate(gj)
        sg = StateGraph(dict)
        noop = lambda state: {}  # noqa: E731
        for node in wf.nodes:
            sg.add_node(node.id, noop)
        cond_nodes = {n.id: n for n in wf.nodes if n.type == "branch.condition"}
        for e in wf.edges:
            if e.from_ not in cond_nodes:
                sg.add_edge(e.from_, e.to)
        for nid, node in cond_nodes.items():
            arms = [(parse_expr(a.when), a.to) for a in node.config.branches]
            default = node.config.else_

            def router(state: dict, _arms=arms, _default=default) -> str:
                from newfan_workflow import EvalContext

                ctx = EvalContext(run_status=state.get("run_status"))
                for cond, to in _arms:
                    if evaluate(cond, ctx):
                        return to
                return _default

            targets = [t for _, t in arms] + [default]
            sg.add_conditional_edges(nid, router, {t: t for t in targets})
        first = wf.nodes[0].id
        sg.add_edge(START, first)
        for node in wf.nodes:
            if node.type.startswith("sink."):
                sg.add_edge(node.id, END)
        return sg.compile()

    times = []
    for _ in range(200):
        t0 = time.perf_counter()
        build(graph_json)
        times.append((time.perf_counter() - t0) * 1000)
    print(f"[4] dynamic build+compile x200: median={statistics.median(times):.2f}ms "
          f"mean={statistics.mean(times):.2f}ms max={max(times):.2f}ms")

    app = build(graph_json)
    out = app.invoke({"run_status": "needs_review"})
    print("[4] compiled graph invoke smoke:", "OK" if isinstance(out, dict) else "NG")


if __name__ == "__main__":
    {"1": probe1, "2": probe2, "3a": probe3_start, "3b": probe3_resume, "4": probe4}[sys.argv[1]]()
