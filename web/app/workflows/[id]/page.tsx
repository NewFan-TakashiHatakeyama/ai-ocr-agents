"use client";

// SCR-07 ノードエディタ（§16 P7）。React Flow（キャンバス）+ rjsf（catalog の
// JSON Schema から config フォームを自動生成）。
// - 保存は常に可（lint error があっても draft は保存できる, §11）
// - lint 指摘は node_id をキャンバス上でハイライト
// - activate は lint の activatable（error=0 + 実装済みのみ + dry-run）で活性
import { use, useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Background,
  Controls,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  type Connection,
  type Edge,
  type Node,
  type NodeChange,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import Form from "@rjsf/core";
import type { RJSFSchema } from "@rjsf/utils";
import validator from "@rjsf/validator-ajv8";

import { AppShell } from "@/components/AppShell";
import { StatusChip } from "@/components/StatusChip";
import { ApiError, api } from "@/lib/api";
import { useToasts } from "@/lib/toast";
import type {
  DryRunResultDto,
  LintFindingDto,
  WorkflowGraphDto,
  WorkflowNodeDto,
} from "@/lib/types";

// ---------- ノードの見た目（カテゴリ色はデザイントークンに寄せる） ----------

const CATEGORY: Record<string, { label: string; color: string }> = {
  source: { label: "トリガー", color: "var(--ok, #2e9e6b)" },
  process: { label: "処理", color: "var(--accent, #3b6ef5)" },
  branch: { label: "分岐", color: "var(--warn, #d98a1f)" },
  transform: { label: "変換", color: "#8a5cd6" },
  sink: { label: "出力", color: "#d65c7a" },
};

const TYPE_LABEL: Record<string, string> = {
  "source.s3_event": "S3 イベント",
  "source.manual": "手動実行",
  "source.schedule": "スケジュール",
  "source.email_attachment": "メール添付",
  "process.classify": "帳票分類",
  "process.extract": "AI-OCR 抽出",
  "branch.condition": "条件分岐",
  "branch.hitl_gate": "HITL ゲート",
  "transform.map_fields": "フィールド変換",
  "sink.db_write": "DB 格納",
  "sink.webhook": "Webhook",
  "sink.file": "ファイル出力",
  "sink.notify": "通知",
};

type FlowNodeData = { wf: WorkflowNodeDto; bad: boolean; implemented: boolean };

function WfNode({ data, selected }: NodeProps<Node<FlowNodeData>>) {
  const cat = CATEGORY[data.wf.type.split(".")[0]] ?? CATEGORY.process;
  return (
    <div
      className={`wfnode${selected ? " sel" : ""}${data.bad ? " bad" : ""}`}
      style={{ borderTopColor: cat.color }}
    >
      <Handle type="target" position={Position.Left} />
      <div className="wfnode-cat" style={{ color: cat.color }}>
        {cat.label}
        {!data.implemented && <span className="wfnode-todo">未実装</span>}
      </div>
      <div className="wfnode-label">{TYPE_LABEL[data.wf.type] ?? data.wf.type}</div>
      <div className="wfnode-id">{data.wf.id}</div>
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

const NODE_TYPES = { wf: WfNode };

// branch.condition の分岐先は config（branches/else）が正。キャンバスには
// 破線ラベル付きで描くだけで、辺としての編集はさせない（§4.1）。
function conditionEdges(nodes: WorkflowNodeDto[]): Edge[] {
  const out: Edge[] = [];
  for (const n of nodes) {
    if (n.type !== "branch.condition") continue;
    const cfg = n.config as { branches?: { when: string; to: string }[]; else?: string };
    (cfg.branches ?? []).forEach((b, i) => {
      out.push({
        id: `cond:${n.id}:${i}`,
        source: n.id,
        target: b.to,
        label: b.when,
        animated: true,
        style: { strokeDasharray: "6 3" },
        deletable: false,
        markerEnd: { type: MarkerType.ArrowClosed },
      });
    });
    if (cfg.else) {
      out.push({
        id: `cond:${n.id}:else`,
        source: n.id,
        target: cfg.else,
        label: "else",
        animated: true,
        style: { strokeDasharray: "6 3" },
        deletable: false,
        markerEnd: { type: MarkerType.ArrowClosed },
      });
    }
  }
  return out;
}

export default function WorkflowEditorPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const qc = useQueryClient();
  const push = useToasts((s) => s.push);

  const { data: wf, error } = useQuery({
    queryKey: ["workflow", id],
    queryFn: () => api.getWorkflow(id),
  });
  const { data: catalog } = useQuery({
    queryKey: ["workflow-catalog"],
    queryFn: () => api.workflowCatalog(),
  });

  // エディタのローカル状態（保存で PUT）。wf 到着時に一度だけ流し込む
  const [name, setName] = useState("");
  const [nodes, setNodes] = useState<WorkflowNodeDto[]>([]);
  const [edges, setEdges] = useState<{ from: string; to: string }[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [findings, setFindings] = useState<LintFindingDto[]>([]);
  const [activatable, setActivatable] = useState(false);
  const [dryRun, setDryRun] = useState<DryRunResultDto | null>(null);
  const [dirty, setDirty] = useState(false);
  const loadedFor = useRef<string | null>(null);

  useEffect(() => {
    if (!wf || loadedFor.current === `${wf.id}:${wf.version}`) return;
    loadedFor.current = `${wf.id}:${wf.version}`;
    setName(wf.name);
    setNodes(wf.graph_json.nodes.map((n, i) => ({ ...n, pos: n.pos ?? [80 + i * 240, 160] })));
    setEdges(wf.graph_json.edges);
    setDirty(false);
  }, [wf]);

  const graph = useCallback(
    (): WorkflowGraphDto => ({ version: 1, nodes, edges }),
    [nodes, edges],
  );

  const badIds = useMemo(
    () => new Set(findings.filter((f) => f.severity === "error" && f.node_id).map((f) => f.node_id)),
    [findings],
  );
  const implemented = useMemo(() => new Set(catalog?.implemented ?? []), [catalog]);

  const rfNodes: Node<FlowNodeData>[] = useMemo(
    () =>
      nodes.map((n) => ({
        id: n.id,
        type: "wf",
        position: { x: n.pos?.[0] ?? 0, y: n.pos?.[1] ?? 0 },
        // controlled モードでは selected をこちらが載せないと選択が即座に外れる
        selected: n.id === selectedId,
        data: { wf: n, bad: badIds.has(n.id), implemented: implemented.has(n.type) },
      })),
    [nodes, badIds, implemented, selectedId],
  );
  const rfEdges: Edge[] = useMemo(
    () => [
      ...edges.map((e) => ({
        id: `e:${e.from}->${e.to}`,
        source: e.from,
        target: e.to,
        markerEnd: { type: MarkerType.ArrowClosed },
      })),
      ...conditionEdges(nodes),
    ],
    [edges, nodes],
  );

  const onNodesChange = useCallback((changes: NodeChange<Node<FlowNodeData>>[]) => {
    // 位置だけ graph_json（pos）へ反映。削除はパレット側のボタンで明示的に行う
    for (const ch of changes) {
      if (ch.type === "position" && ch.position) {
        const { x, y } = ch.position;
        setNodes((ns) =>
          ns.map((n) => (n.id === ch.id ? { ...n, pos: [Math.round(x), Math.round(y)] } : n)),
        );
        setDirty(true);
      }
      if (ch.type === "select") setSelectedId(ch.selected ? ch.id : null);
    }
  }, []);

  const onConnect = useCallback(
    (c: Connection) => {
      if (!c.source || !c.target) return;
      const src = nodes.find((n) => n.id === c.source);
      if (src?.type === "branch.condition") {
        push({ kind: "warn", message: "条件分岐の行き先は config（branches / else）で指定します。" });
        return;
      }
      setEdges((es) =>
        es.some((e) => e.from === c.source && e.to === c.target)
          ? es
          : [...es, { from: c.source, to: c.target }],
      );
      setDirty(true);
    },
    [nodes, push],
  );

  const onEdgesDelete = useCallback((deleted: Edge[]) => {
    setEdges((es) =>
      es.filter((e) => !deleted.some((d) => d.source === e.from && d.target === e.to)),
    );
    setDirty(true);
  }, []);

  const addNode = (type: string) => {
    const prefix = type.split(".")[1].replace(/_/g, "").slice(0, 4);
    let i = 1;
    while (nodes.some((n) => n.id === `${prefix}${i}`)) i += 1;
    setNodes((ns) => [
      ...ns,
      { id: `${prefix}${i}`, type, config: {}, pos: [80 + ns.length * 230, 330 + (ns.length % 2) * 110] },
    ]);
    setDirty(true);
  };

  const removeSelected = () => {
    if (!selectedId) return;
    setNodes((ns) => ns.filter((n) => n.id !== selectedId));
    setEdges((es) => es.filter((e) => e.from !== selectedId && e.to !== selectedId));
    setSelectedId(null);
    setDirty(true);
  };

  const save = useMutation({
    mutationFn: () => api.putWorkflow(id, name, graph()),
    onSuccess: async (w) => {
      loadedFor.current = `${w.id}:${w.version}`;
      setDirty(false);
      qc.invalidateQueries({ queryKey: ["workflow", id] });
      push({ kind: "ok", message: `保存しました（v${w.version}・draft）。` });
      // 保存のたびに lint を回す（指摘の鮮度 = エディタの信頼性）
      const lint = await api.lintWorkflow(id);
      setFindings(lint.findings);
      setActivatable(lint.activatable);
    },
    onError: (e) =>
      push({ kind: "warn", message: `保存に失敗しました（${(e as Error).message}）。` }),
  });

  const runLint = useMutation({
    mutationFn: () => api.lintWorkflow(id, graph()),
    onSuccess: (r) => {
      setFindings(r.findings);
      setActivatable(r.activatable && !dirty);
      push({
        kind: r.findings.some((f) => f.severity === "error") ? "warn" : "ok",
        message: `lint: エラー ${r.findings.filter((f) => f.severity === "error").length} / 警告 ${r.findings.filter((f) => f.severity === "warning").length}`,
      });
    },
  });

  const runDryRun = useMutation({
    mutationFn: () => api.dryRunWorkflow(id),
    onSuccess: (r) => setDryRun(r),
    onError: (e) =>
      push({ kind: "warn", message: `dry-run に失敗しました（${(e as Error).message}）。` }),
  });

  const activate = useMutation({
    mutationFn: () => api.activateWorkflow(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["workflow", id] });
      loadedFor.current = null;
      push({ kind: "ok", message: "有効化しました（版が固定されます）。" });
    },
    onError: (e) =>
      push({ kind: "warn", message: `有効化できません（${(e as Error).message}）。` }),
  });

  const pause = useMutation({
    mutationFn: () => api.pauseWorkflow(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["workflow", id] });
      loadedFor.current = null;
      push({ kind: "ok", message: "停止しました。" });
    },
  });

  const selected = nodes.find((n) => n.id === selectedId) ?? null;
  const selectedSchema = selected
    ? ((catalog?.types[selected.type] ?? null) as RJSFSchema | null)
    : null;

  if (error instanceof ApiError && error.status === 403) {
    return (
      <AppShell active="workflows">
        <div className="access-denied">
          <div style={{ fontSize: 40 }}>🔒</div>
          <h2>権限がありません</h2>
          <p>この画面は管理者（admin）のみ利用できます。</p>
        </div>
      </AppShell>
    );
  }

  return (
    <AppShell active="workflows">
      <div className="topbar">
        <Link href="/workflows" className="btn ghost">
          ← 一覧
        </Link>
        <input
          className="wf-title-input"
          value={name}
          onChange={(e) => {
            setName(e.target.value);
            setDirty(true);
          }}
        />
        {wf && <StatusChip status={wf.status} />}
        {wf && <span className="sub">v{wf.version}</span>}
        {dirty && <span className="sub" style={{ color: "var(--warn, #d98a1f)" }}>未保存</span>}
        <div style={{ marginLeft: "auto", display: "flex", gap: 8 }}>
          <button className="btn" onClick={() => runLint.mutate()}>
            lint
          </button>
          <button className="btn" onClick={() => save.mutate()} disabled={save.isPending}>
            保存
          </button>
          <button
            className="btn"
            onClick={() => runDryRun.mutate()}
            disabled={dirty}
            title={dirty ? "先に保存してください" : "sink のプレビュー（実 SQL / ペイロード）"}
          >
            dry-run
          </button>
          {wf?.status === "active" ? (
            <button className="btn" onClick={() => pause.mutate()}>
              停止
            </button>
          ) : (
            <button
              className="btn primary"
              onClick={() => activate.mutate()}
              disabled={dirty || !activatable}
              title={
                dirty
                  ? "先に保存してください"
                  : activatable
                    ? "有効化（版の固定）"
                    : "lint（保存後）が error 0 になると有効化できます"
              }
            >
              有効化
            </button>
          )}
        </div>
      </div>

      <div className="wf-editor">
        {/* パレット */}
        <div className="wf-palette">
          <div className="wf-pane-title">ノード（カタログ）</div>
          {Object.entries(CATEGORY).map(([cat, meta]) => (
            <div key={cat}>
              <div className="wf-palette-cat" style={{ color: meta.color }}>
                {meta.label}
              </div>
              {Object.keys(catalog?.types ?? {})
                .filter((t) => t.startsWith(cat + "."))
                .map((t) => (
                  <button key={t} className="wf-palette-item" onClick={() => addNode(t)}>
                    {TYPE_LABEL[t] ?? t}
                    {!implemented.has(t) && <span className="wfnode-todo">未実装</span>}
                  </button>
                ))}
            </div>
          ))}
          <button className="btn danger" style={{ marginTop: 12 }} onClick={removeSelected} disabled={!selectedId}>
            選択ノードを削除
          </button>
        </div>

        {/* キャンバス */}
        <div className="wf-canvas">
          <ReactFlow
            nodes={rfNodes}
            edges={rfEdges}
            nodeTypes={NODE_TYPES}
            onNodesChange={onNodesChange}
            onConnect={onConnect}
            onEdgesDelete={onEdgesDelete}
            deleteKeyCode={["Delete", "Backspace"]}
            fitView
            proOptions={{ hideAttribution: true }}
          >
            <Background gap={16} />
            <Controls showInteractive={false} />
          </ReactFlow>
        </div>

        {/* 右パネル: config フォーム + lint + dry-run */}
        <div className="wf-side">
          {selected && selectedSchema ? (
            <div className="wf-config">
              <div className="wf-pane-title">
                {TYPE_LABEL[selected.type] ?? selected.type}
                <span className="sub" style={{ marginLeft: 6 }}>{selected.id}</span>
              </div>
              <Form
                schema={selectedSchema}
                validator={validator}
                formData={selected.config}
                liveValidate={false}
                showErrorList={false}
                onChange={({ formData }) => {
                  setNodes((ns) =>
                    ns.map((n) =>
                      n.id === selected.id
                        ? { ...n, config: (formData ?? {}) as Record<string, unknown> }
                        : n,
                    ),
                  );
                  setDirty(true);
                }}
              >
                {/* 送信ボタン不要（保存はツールバー） */}
                <span />
              </Form>
            </div>
          ) : (
            <div className="wf-config empty">ノードを選択すると設定フォームが出ます。</div>
          )}

          <div className="wf-findings">
            <div className="wf-pane-title">lint 指摘</div>
            {findings.length === 0 && <div className="sub">指摘はありません。</div>}
            {findings.map((f, i) => (
              <button
                key={i}
                className={`wf-finding ${f.severity}`}
                onClick={() => f.node_id && setSelectedId(f.node_id)}
              >
                <b>{f.rule}</b> {f.message}
                {f.node_id ? `（${f.node_id}）` : ""}
              </button>
            ))}
          </div>

          {dryRun && (
            <div className="wf-findings">
              <div className="wf-pane-title">dry-run（プレビュー = 実 SQL）</div>
              {dryRun.sinks.map((s2) => (
                <div key={s2.node_id} className={`wf-finding ${s2.ok ? "warning" : "error"}`}>
                  <b>{s2.node_id}</b> {s2.node_type} {s2.ok ? "OK" : `NG: ${s2.error}`}
                  {s2.sql && <pre className="wf-sql">{s2.sql}</pre>}
                  {s2.payload && <pre className="wf-sql">{JSON.stringify(s2.payload, null, 1)}</pre>}
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </AppShell>
  );
}
