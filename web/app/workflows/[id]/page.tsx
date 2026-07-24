"use client";

// SCR-07 ノードエディタ（§16 P7）。React Flow（キャンバス）+ rjsf（catalog の
// JSON Schema から config フォームを自動生成）。
// - 保存は常に可（lint error があっても draft は保存できる, §11）
// - lint 指摘は node_id をキャンバス上でハイライト
// - activate は lint の activatable（error=0 + 実装済みのみ + dry-run）で活性
//
// ドラッグ性能: ノード位置は React Flow の state（useNodesState）が正で、
// graph_json へは保存時にまとめて書き戻す。ドメイン配列をドラッグ毎フレーム
// 再構築すると全ノードが毎フレーム再描画されカクつくため、この分離が必須。
import {
  createContext,
  use,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Background,
  Controls,
  Handle,
  MarkerType,
  MiniMap,
  Position,
  ReactFlow,
  ReactFlowProvider,
  useNodesState,
  useReactFlow,
  type Connection,
  type Edge,
  type EdgeChange,
  type Node,
  type NodeChange,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import Form from "@rjsf/core";
import type { RJSFSchema, UiSchema } from "@rjsf/utils";
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

import { CATEGORY, NodeIcon, TYPE_LABEL } from "../node-icons";
import { fieldLabel, jaTranslateString, localizeSchema, pruneEmpty } from "../field-labels";

// 複数行で書くフィールドは textarea にする（1行 input だと窮屈で読みにくい）
const UI_SCHEMA_BY_TYPE: Record<string, UiSchema> = {
  "sink.notify": { template: { "ui:widget": "textarea", "ui:options": { rows: 3 } } },
  "sink.webhook": { payload_template: { "ui:widget": "textarea", "ui:options": { rows: 4 } } },
};

// ---------- ノードの見た目 ----------

type WfFlowNode = Node<{ wf: WorkflowNodeDto }, "wf">;

// lint 結果・catalog はノード data に埋めず context で配る。埋めると更新のたびに
// 全ノード data を同期し直す羽目になる（ドラッグと干渉しやすい）。
const EditorMeta = createContext<{ bad: Set<string>; implemented: Set<string> }>({
  bad: new Set(),
  implemented: new Set(),
});

function WfNode({ id, data, selected }: NodeProps<WfFlowNode>) {
  const meta = useContext(EditorMeta);
  const cat = CATEGORY[data.wf.type.split(".")[0]] ?? CATEGORY.process;
  const bad = meta.bad.has(id);
  const isSource = data.wf.type.startsWith("source.");
  return (
    <div className={`wfnode${selected ? " sel" : ""}${bad ? " bad" : ""}`}>
      {/* トリガーへは新規接続を張らせない。ただし Handle 自体を消すと、source への
          流入辺を持つ既存グラフを開いた時に辺が不可視・削除不能になるため描画は残す */}
      <Handle
        type="target"
        position={Position.Left}
        isConnectable={!isSource}
        style={isSource ? { opacity: 0 } : undefined}
      />
      <span className="wfnode-icon" style={{ background: cat.color }}>
        <NodeIcon type={data.wf.type} size={16} />
      </span>
      <div className="wfnode-body">
        <div className="wfnode-label">{TYPE_LABEL[data.wf.type] ?? data.wf.type}</div>
        <div className="wfnode-meta">
          <span className="wfnode-cat" style={{ color: cat.color }}>
            {cat.label}
          </span>
          <span className="wfnode-id">{id}</span>
          {!meta.implemented.has(data.wf.type) && <span className="wfnode-todo">未実装</span>}
        </div>
      </div>
      {bad && (
        <span className="wfnode-warnmark" title="lint エラーがあります">
          !
        </span>
      )}
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

const NODE_TYPES = { wf: WfNode };

// branch.condition の分岐先は config（branches/else）が正。キャンバスには
// 破線ラベル付きで描くだけで、辺としての編集はさせない（§4.1）。
type CondSpec = {
  id: string;
  config: { branches?: { when: string; to: string }[]; else?: string };
};

function conditionEdges(conds: CondSpec[], validIds: Set<string>): Edge[] {
  const out: Edge[] = [];
  for (const n of conds) {
    const mk = (key: string, to: string, label: string) => {
      if (!validIds.has(to)) return; // 参照先が消えた分岐は描かない（lint が指摘する）
      out.push({
        id: `cond:${n.id}:${key}`,
        source: n.id,
        target: to,
        label,
        animated: true,
        style: { strokeDasharray: "6 3" },
        deletable: false,
        markerEnd: { type: MarkerType.ArrowClosed },
      });
    };
    (n.config.branches ?? []).forEach((b, i) => mk(String(i), b.to, b.when));
    if (n.config.else) mk("else", n.config.else, "else");
  }
  return out;
}

const DND_MIME = "application/newfan-wf-node";

// エッジ id。素朴な `e:from->to` はノード id が '->' を含むと衝突するため
// JSON エスケープで区切りを保護する
const edgeId = (e: { from: string; to: string }) => `e:${JSON.stringify([e.from, e.to])}`;

// rjsf はマウント時にも onChange を発火し、キー順も保存済み config と一致しない。
// 「選択しただけで未保存」を防ぐため、順序に依存しない同値判定で実変更だけ拾う。
function deepEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (typeof a !== typeof b || a === null || b === null) return false;
  if (Array.isArray(a) || Array.isArray(b)) {
    if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false;
    return a.every((v, i) => deepEqual(v, b[i]));
  }
  if (typeof a === "object") {
    const ka = Object.keys(a as object);
    const kb = Object.keys(b as object);
    if (ka.length !== kb.length) return false;
    return ka.every((k) =>
      deepEqual((a as Record<string, unknown>)[k], (b as Record<string, unknown>)[k]),
    );
  }
  return false;
}

function WorkflowEditor({ id }: { id: string }) {
  const qc = useQueryClient();
  const push = useToasts((s) => s.push);
  const { screenToFlowPosition } = useReactFlow();

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
  const [rfNodes, setRfNodes, onNodesChange] = useNodesState<WfFlowNode>([]);
  const [edges, setEdges] = useState<{ from: string; to: string }[]>([]);
  const [selEdgeIds, setSelEdgeIds] = useState<ReadonlySet<string>>(new Set());
  const [findings, setFindings] = useState<LintFindingDto[]>([]);
  const [activatable, setActivatable] = useState(false);
  const [dryRun, setDryRun] = useState<DryRunResultDto | null>(null);
  const [dirty, setDirty] = useState(false);
  const loadedFor = useRef<string | null>(null);

  useEffect(() => {
    if (!wf || loadedFor.current === `${wf.id}:${wf.version}`) return;
    loadedFor.current = `${wf.id}:${wf.version}`;
    setName(wf.name);
    setRfNodes(
      wf.graph_json.nodes.map((n, i) => {
        const pos = n.pos ?? [80 + i * 240, 160];
        return {
          id: n.id,
          type: "wf" as const,
          position: { x: pos[0], y: pos[1] },
          data: { wf: n },
        };
      }),
    );
    // サーバ由来の同一 {from,to} 重複は React key 衝突になるため先頭のみ残す
    const seen = new Set<string>();
    setEdges(
      wf.graph_json.edges.filter((e) => {
        const k = edgeId(e);
        if (seen.has(k)) return false;
        seen.add(k);
        return true;
      }),
    );
    setSelEdgeIds(new Set());
    setDirty(false);
  }, [wf, setRfNodes]);

  // 保存・lint に渡す graph_json はエディタ state からその場で組み立てる
  const graph = useCallback(
    (): WorkflowGraphDto => ({
      version: 1,
      nodes: rfNodes.map((rn) => ({
        ...rn.data.wf,
        // 空欄の Optional 項目（""）は送らない（未入力=None として扱う）
        config: pruneEmpty(rn.data.wf.config),
        pos: [Math.round(rn.position.x), Math.round(rn.position.y)] as [number, number],
      })),
      edges,
    }),
    [rfNodes, edges],
  );

  const badIds = useMemo(
    () =>
      new Set(
        findings
          .filter((f) => f.severity === "error" && f.node_id)
          .map((f) => f.node_id as string),
      ),
    [findings],
  );
  const implemented = useMemo(() => new Set(catalog?.implemented ?? []), [catalog]);
  const editorMeta = useMemo(() => ({ bad: badIds, implemented }), [badIds, implemented]);

  // 条件分岐の派生エッジ。ドラッグ中は config が変わらないため、内容の
  // シグネチャで memo してフレーム毎の再計算・エッジ再生成を避ける。
  const condSig = JSON.stringify({
    ids: rfNodes.map((n) => n.id),
    conds: rfNodes
      .filter((n) => n.data.wf.type === "branch.condition")
      .map((n) => ({ id: n.id, config: n.data.wf.config })),
  });
  const rfEdges: Edge[] = useMemo(() => {
    const { ids, conds } = JSON.parse(condSig) as { ids: string[]; conds: CondSpec[] };
    const valid = new Set(ids);
    return [
      ...edges
        .filter((e) => valid.has(e.from) && valid.has(e.to))
        .map((e) => ({
          id: edgeId(e),
          source: e.from,
          target: e.to,
          selected: selEdgeIds.has(edgeId(e)),
          markerEnd: { type: MarkerType.ArrowClosed },
        })),
      ...conditionEdges(conds, valid),
    ];
  }, [edges, condSig, selEdgeIds]);

  // エッジは domain state（from/to）が正。RF からは選択だけ受けて Delete を効かせる
  const onEdgesChange = useCallback((changes: EdgeChange[]) => {
    setSelEdgeIds((prev) => {
      let next: Set<string> | null = null;
      for (const ch of changes) {
        if (ch.type === "select") {
          next ??= new Set(prev);
          if (ch.selected) next.add(ch.id);
          else next.delete(ch.id);
        } else if (ch.type === "remove") {
          // 掃除しないと同じ from→to を張り直した時に選択状態で復活し、
          // 次の Delete で巻き添え削除される
          next ??= new Set(prev);
          next.delete(ch.id);
        }
      }
      return next ?? prev;
    });
  }, []);

  const onEdgesDelete = useCallback((deleted: Edge[]) => {
    setEdges((es) =>
      es.filter((e) => !deleted.some((d) => d.source === e.from && d.target === e.to)),
    );
    setDirty(true);
  }, []);

  const onNodesDelete = useCallback((deleted: Node[]) => {
    const ids = new Set(deleted.map((d) => d.id));
    setEdges((es) => es.filter((e) => !ids.has(e.from) && !ids.has(e.to)));
    setDirty(true);
  }, []);

  const onConnect = useCallback(
    (c: Connection) => {
      if (!c.source || !c.target) return;
      const src = rfNodes.find((n) => n.id === c.source);
      if (src?.data.wf.type === "branch.condition") {
        push({ kind: "warn", message: "条件分岐の行き先は config（branches / else）で指定します。" });
        return;
      }
      if (rfNodes.find((n) => n.id === c.target)?.data.wf.type.startsWith("source.")) {
        push({ kind: "warn", message: "トリガーへは接続できません。" });
        return;
      }
      // 既存と同じ辺は何も変えない（dirty を立てると dry-run/有効化が無意味に塞がる）
      if (edges.some((e) => e.from === c.source && e.to === c.target)) return;
      const from = c.source;
      const to = c.target;
      setEdges((es) => [...es, { from, to }]);
      setDirty(true);
    },
    [rfNodes, edges, push],
  );

  const addNode = useCallback(
    (type: string, pos?: [number, number]) => {
      setRfNodes((ns) => {
        const prefix = type.split(".")[1].replace(/_/g, "").slice(0, 4);
        let i = 1;
        while (ns.some((n) => n.id === `${prefix}${i}`)) i += 1;
        const nid = `${prefix}${i}`;
        const p = pos ?? [80 + ns.length * 230, 330 + (ns.length % 2) * 110];
        return [
          // 追加ノードを選択状態にして右の設定フォームをすぐ開く
          ...ns.map((n) => (n.selected ? { ...n, selected: false } : n)),
          {
            id: nid,
            type: "wf" as const,
            position: { x: p[0], y: p[1] },
            selected: true,
            data: { wf: { id: nid, type, config: {} } },
          },
        ];
      });
      setDirty(true);
    },
    [setRfNodes],
  );

  // パレット → キャンバスへのドラッグ配置（クリック追加も残す）
  const onDrop = useCallback(
    (e: React.DragEvent) => {
      // 先にキャンセルする。OS からのファイルドロップでページ遷移して
      // 未保存グラフが消えるのを防ぐ（パレット以外のドロップは無視するだけ）
      e.preventDefault();
      const type = e.dataTransfer.getData(DND_MIME);
      if (!type) return;
      const p = screenToFlowPosition({ x: e.clientX, y: e.clientY });
      // つまんだ位置がカード中央になるよう半分ずらす
      addNode(type, [Math.round(p.x) - 90, Math.round(p.y) - 24]);
    },
    [screenToFlowPosition, addNode],
  );

  const removeSelected = () => {
    const sel = rfNodes.filter((n) => n.selected);
    if (!sel.length) return;
    const ids = new Set(sel.map((n) => n.id));
    setRfNodes((ns) => ns.filter((n) => !ids.has(n.id)));
    setEdges((es) => es.filter((e) => !ids.has(e.from) && !ids.has(e.to)));
    // ボタン削除は RF を経由しない（remove change が来ない）ので選択集合も自前で掃除
    setSelEdgeIds((prev) => {
      const next = new Set(prev);
      for (const e of edges) if (ids.has(e.from) || ids.has(e.to)) next.delete(edgeId(e));
      return next;
    });
    setDirty(true);
  };

  // ドラッグ終了と矢印キー移動はどちらも dragging:false の position change として
  // 届く。onNodeDragStop だけではキーボード移動が「未保存」にならない
  const handleNodesChange = useCallback(
    (changes: NodeChange<WfFlowNode>[]) => {
      onNodesChange(changes);
      if (changes.some((ch) => ch.type === "position" && !ch.dragging)) setDirty(true);
    },
    [onNodesChange],
  );

  const selectNode = (nid: string) =>
    setRfNodes((ns) =>
      ns.map((n) => (n.selected !== (n.id === nid) ? { ...n, selected: n.id === nid } : n)),
    );

  // E4001（config スキーマ不正）を「どのノードのどの項目か」の日本語文へ。
  // 保存・点検が失敗したとき、原因が分からないと利用者は直しようがない。
  const schemaErrorMessage = (e: unknown, prefix: string): string => {
    const errs =
      (e instanceof ApiError &&
        (e.details as { errors?: { loc?: string; msg?: string }[] } | undefined)?.errors) ||
      null;
    if (errs?.length) {
      const g = graph();
      const parts = errs.slice(0, 2).map((er) => {
        const loc = er.loc ?? "";
        const m = /^nodes\.(\d+)\./.exec(loc);
        const node = m ? g.nodes[Number(m[1])] : undefined;
        const nid = node?.id ?? loc;
        const nodeName = node ? nodeLabelById.get(node.id) : undefined;
        // フィールド名も日本語ラベルへ（フォームと一致させる）。array 添字は1つ上を使う
        const segs = loc.split(".");
        const rawField = /^\d+$/.test(segs[segs.length - 1] ?? "")
          ? (segs[segs.length - 2] ?? "")
          : (segs[segs.length - 1] ?? "");
        const schemaTitle = node
          ? ((catalog?.types[node.type] as { title?: string } | undefined)?.title)
          : undefined;
        const field = fieldLabel(schemaTitle, rawField) ?? rawField;
        return `${nodeName ? `${nodeName}（${nid}）` : nid} の「${field}」: ${er.msg}`;
      });
      return `${prefix} — ${parts.join(" / ")}`;
    }
    return `${prefix}（${(e as Error).message}）。`;
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
    onError: (e) => push({ kind: "warn", message: schemaErrorMessage(e, "保存できません") }),
  });

  const runLint = useMutation({
    mutationFn: () => api.lintWorkflow(id, graph()),
    onSuccess: (r) => {
      setFindings(r.findings);
      setActivatable(r.activatable && !dirty);
      const errN = r.findings.filter((f) => f.severity === "error").length;
      const warnN = r.findings.filter((f) => f.severity === "warning").length;
      push({
        kind: errN > 0 ? "warn" : "ok",
        message:
          errN + warnN === 0
            ? "点検しました。問題は見つかりませんでした。"
            : `点検しました。エラー ${errN} 件 / 警告 ${warnN} 件`,
      });
    },
    // config が未入力などでグラフ自体が検証を通らないと 422。何も反応しないと
    // 利用者は原因が分からないため、どのノードのどの項目かを保存と同じ形で示す
    onError: (e) => push({ kind: "warn", message: schemaErrorMessage(e, "点検できません") }),
  });

  const runDryRun = useMutation({
    mutationFn: () => api.dryRunWorkflow(id),
    onSuccess: (r) => setDryRun(r),
    onError: (e) => push({ kind: "warn", message: schemaErrorMessage(e, "出力を確認できません") }),
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
      // loadedFor は触らない。pause は version を変えないため、リフェッチ後も
      // ロード effect のガード（id:version 一致）が効いて未保存編集が破棄されない。
      // status 表示は StatusChip が wf.status を直接読むので invalidate だけで更新される
      qc.invalidateQueries({ queryKey: ["workflow", id] });
      push({ kind: "ok", message: "停止しました。" });
    },
  });

  const selected = rfNodes.find((n) => n.selected)?.data.wf ?? null;
  const rawSchema = selected
    ? ((catalog?.types[selected.type] ?? null) as RJSFSchema | null)
    : null;
  // catalog は不変なので type ごとに一度だけ日本語化する（毎レンダーのクローンを避ける）
  const selectedSchema = useMemo(
    () => (rawSchema ? localizeSchema(rawSchema) : null),
    [rawSchema],
  );
  const selectedUiSchema = selected ? UI_SCHEMA_BY_TYPE[selected.type] : undefined;

  // 点検結果・出力プレビューで node_id を日本語ノード名に読み替えるための表
  const nodeLabelById = useMemo(() => {
    const m = new Map<string, string>();
    for (const n of rfNodes) m.set(n.id, TYPE_LABEL[n.data.wf.type] ?? n.data.wf.type);
    return m;
  }, [rfNodes]);

  // DB 格納の「どの列にどんな値が入るか」を、直前の map_fields からテーブル用に組む。
  // dry-run（プレビュー=実 SQL）と同じ前段走査（_map_preds）に合わせる。
  const dbWriteTable = (
    sinkId: string,
  ): { rows: { column: string; source: string; note?: string }[]; target?: string; method: string } | null => {
    const sink = rfNodes.find((n) => n.id === sinkId)?.data.wf;
    if (!sink || sink.type !== "sink.db_write") return null;
    const cfg = sink.config as { table?: string; mode?: string; keys?: string[] };
    const preds = new Set<string>();
    for (const e of edges) if (e.to === sinkId) preds.add(e.from);
    for (const n of rfNodes) {
      if (n.data.wf.type !== "branch.condition") continue;
      const c = n.data.wf.config as { branches?: { to: string }[]; else?: string };
      const targets = new Set([...(c.branches ?? []).map((b) => b.to), ...(c.else ? [c.else] : [])]);
      if (targets.has(sinkId)) preds.add(n.id);
    }
    const rows: { column: string; source: string; note?: string }[] = [];
    for (const pid of [...preds].sort()) {
      const p = rfNodes.find((n) => n.id === pid)?.data.wf;
      if (p?.type !== "transform.map_fields") continue;
      const mc = p.config as {
        mappings?: { from?: string; to: string; const?: string; format?: string; mask?: boolean }[];
      };
      for (const mp of mc.mappings ?? []) {
        if (rows.some((r) => r.column === mp.to)) continue;
        const source =
          mp.const != null && mp.const !== ""
            ? `固定値「${mp.const}」`
            : mp.from
              ? `抽出値「${mp.from}」`
              : "（未設定）";
        const notes: string[] = [];
        if (mp.format) notes.push(`書式: ${mp.format}`);
        if (mp.mask) notes.push("マスク");
        rows.push({ column: mp.to, source, note: notes.join(" / ") || undefined });
      }
    }
    if ((cfg.mode ?? "") === "insert") {
      rows.push({ column: "nf_write_key", source: "自動（重複防止キー）" });
    }
    const method =
      cfg.mode === "upsert"
        ? `追加または更新（重複判定: ${(cfg.keys ?? []).join(", ") || "―"}）`
        : "新規追加";
    return { rows, target: cfg.table, method };
  };

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

  if (!wf) {
    // 本体ロード前は編集 UI を出さない。到着時に graph_json を無条件で流し込むため、
    // ロード前に行った編集（パレット追加・タイトル入力）は黙って消えてしまう
    return (
      <AppShell active="workflows">
        <div className="sub" style={{ padding: 24 }}>読み込み中…</div>
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
          <button
            className="btn"
            onClick={() => runLint.mutate()}
            title="設定の不備や配線ミスを点検します"
          >
            設定を点検
          </button>
          <button className="btn" onClick={() => save.mutate()} disabled={save.isPending}>
            保存
          </button>
          <button
            className="btn"
            onClick={() => runDryRun.mutate()}
            disabled={dirty}
            title={dirty ? "先に保存してください" : "出力ノードが実際に書き込む・送信する内容を、実行せずに確認します"}
          >
            出力を確認
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
                    ? "このワークフローを有効にして自動実行を開始します"
                    : "「設定を点検」でエラーが 0 件になると有効にできます"
              }
            >
              有効にする
            </button>
          )}
        </div>
      </div>

      <div className="wf-editor">
        {/* パレット */}
        <div className="wf-palette">
          <div className="wf-pane-title">ノード（カタログ）</div>
          <div className="wf-palette-hint">クリック、またはキャンバスへドラッグで追加</div>
          {Object.entries(CATEGORY).map(([cat, meta]) => (
            <div key={cat}>
              <div className="wf-palette-cat" style={{ color: meta.color }}>
                {meta.label}
              </div>
              {Object.keys(catalog?.types ?? {})
                .filter((t) => t.startsWith(cat + "."))
                .map((t) => (
                  <button
                    key={t}
                    className="wf-palette-item"
                    draggable
                    onDragStart={(e) => {
                      e.dataTransfer.setData(DND_MIME, t);
                      e.dataTransfer.effectAllowed = "move";
                    }}
                    onClick={() => addNode(t)}
                  >
                    <span className="wf-palette-ic" style={{ background: meta.color }}>
                      <NodeIcon type={t} size={13} />
                    </span>
                    <span className="wf-palette-name">{TYPE_LABEL[t] ?? t}</span>
                    {!implemented.has(t) && <span className="wfnode-todo">未実装</span>}
                  </button>
                ))}
            </div>
          ))}
          <button
            className="btn danger"
            style={{ marginTop: 12 }}
            onClick={removeSelected}
            disabled={!rfNodes.some((n) => n.selected)}
          >
            選択ノードを削除
          </button>
        </div>

        {/* キャンバス */}
        <div
          className="wf-canvas"
          onDragOver={(e) => {
            e.preventDefault();
            // 外来ドラッグ（OS のファイル等）にはドロップ不可カーソルを示す
            if (!e.dataTransfer.types.includes(DND_MIME)) e.dataTransfer.dropEffect = "none";
          }}
          onDrop={onDrop}
        >
          <EditorMeta.Provider value={editorMeta}>
            <ReactFlow
              nodes={rfNodes}
              edges={rfEdges}
              nodeTypes={NODE_TYPES}
              onNodesChange={handleNodesChange}
              onNodesDelete={onNodesDelete}
              onEdgesChange={onEdgesChange}
              onEdgesDelete={onEdgesDelete}
              onConnect={onConnect}
              deleteKeyCode={["Delete", "Backspace"]}
              connectionRadius={38}
              fitView
              fitViewOptions={{ padding: 0.25, maxZoom: 1.2 }}
              proOptions={{ hideAttribution: true }}
            >
              <Background gap={16} />
              <Controls showInteractive={false} />
              <MiniMap
                className="wf-minimap"
                pannable
                zoomable
                nodeColor={(n) =>
                  CATEGORY[(n as WfFlowNode).data.wf.type.split(".")[0]]?.color ?? "#8888"
                }
              />
            </ReactFlow>
          </EditorMeta.Provider>
        </div>

        {/* 右パネル: config フォーム + lint + dry-run */}
        <div className="wf-side">
          {selected && selectedSchema ? (
            <div className="wf-config">
              <div className="wf-pane-title">
                設定：{TYPE_LABEL[selected.type] ?? selected.type}
                <span className="sub" style={{ marginLeft: 6 }}>{selected.id}</span>
              </div>
              <Form
                key={selected.id}
                schema={selectedSchema}
                uiSchema={selectedUiSchema}
                translateString={jaTranslateString}
                validator={validator}
                formData={selected.config}
                liveValidate={false}
                showErrorList={false}
                onChange={({ formData }, fieldId) => {
                  const next = (formData ?? {}) as Record<string, unknown>;
                  if (deepEqual(next, selected.config)) return;
                  const apply = () =>
                    setRfNodes((ns) =>
                      ns.map((n) =>
                        n.id === selected.id
                          ? { ...n, data: { wf: { ...n.data.wf, config: next } } }
                          : n,
                      ),
                    );
                  if (fieldId === undefined) {
                    // fieldId 無しはユーザー操作ではなく rjsf の default 補完で、
                    // Form の constructor（= render 中）から同期で呼ばれる。そのまま
                    // setState すると React の「render 中に他コンポーネントを更新」
                    // エラーになるためマイクロタスクへ逃がす。補完はバックエンド
                    // 既定値と同義なので「未保存」にもしない。
                    queueMicrotask(apply);
                  } else {
                    apply();
                    setDirty(true);
                  }
                }}
              >
                {/* 送信ボタン不要（保存はツールバー） */}
                <span />
              </Form>
            </div>
          ) : (
            <div className="wf-config empty">
              キャンバスのノードを選ぶと、ここに設定が表示されます。
            </div>
          )}

          <div className="wf-findings">
            <div className="wf-pane-title">点検結果</div>
            {findings.length === 0 && (
              <div className="sub">まだ点検していません（「設定を点検」を押してください）。</div>
            )}
            {findings.map((f, i) => (
              <button
                key={i}
                className={`wf-finding ${f.severity}`}
                onClick={() => f.node_id && selectNode(f.node_id)}
                title={f.node_id ? "クリックで該当ノードを選択" : undefined}
              >
                <span className={`wf-sev ${f.severity}`}>
                  {f.severity === "error" ? "エラー" : "警告"}
                </span>
                {f.message}
                {f.node_id && <span className="wf-nodetag">{nodeLabelById.get(f.node_id) ?? f.node_id}</span>}
              </button>
            ))}
          </div>

          {dryRun && (
            <div className="wf-findings">
              <div className="wf-pane-title">出力プレビュー</div>
              <div className="sub" style={{ marginBottom: 6 }}>
                実行はせず、DB 格納・Webhook が書き込む・送信する内容を確認できます
                （通知・ファイル出力はプレビュー対象外）。
              </div>
              {dryRun.sinks.length === 0 && (
                <div className="sub">
                  {rfNodes.some((n) =>
                    ["sink.notify", "sink.file"].includes(n.data.wf.type),
                  )
                    ? "通知・ファイル出力ノードは、このプレビューの対象外です（DB 格納・Webhook のみ表示します）。"
                    : "プレビュー対象の出力ノード（DB 格納・Webhook）がありません。"}
                </div>
              )}
              {dryRun.sinks.map((s2) => (
                <div key={s2.node_id} className={`wf-preview ${s2.ok ? "ok" : "ng"}`}>
                  <div className="wf-preview-head">
                    <span className="wf-preview-node">
                      {TYPE_LABEL[s2.node_type] ?? s2.node_type}
                      <span className="wf-nodetag">{s2.node_id}</span>
                    </span>
                    <span className={`wf-sev ${s2.ok ? "ok" : "error"}`}>
                      {s2.ok ? "OK" : "エラー"}
                    </span>
                  </div>
                  {!s2.ok && s2.error && <div className="wf-preview-err">{s2.error}</div>}
                  {(() => {
                    // DB 格納は SQL ではなくテーブル形式で「どの列に何が入るか」を見せる
                    const t = s2.ok && s2.node_type === "sink.db_write" ? dbWriteTable(s2.node_id) : null;
                    if (!t) return null;
                    return (
                      <>
                        <div className="wf-preview-target">
                          書き込み先：<b>{t.target ?? "（未設定）"}</b>
                          <span className="wf-nodetag">{t.method}</span>
                        </div>
                        <table className="wf-dbtable">
                          <thead>
                            <tr>
                              <th>列（テーブルの項目）</th>
                              <th>入る値</th>
                            </tr>
                          </thead>
                          <tbody>
                            {t.rows.map((r) => (
                              <tr key={r.column}>
                                <td className="wf-dbcol">{r.column}</td>
                                <td>
                                  {r.source}
                                  {r.note && <span className="wf-dbnote">{r.note}</span>}
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                        {s2.sql && (
                          <details className="wf-sqldetails">
                            <summary>SQL を表示（上級者向け）</summary>
                            <pre className="wf-sql">{s2.sql}</pre>
                          </details>
                        )}
                      </>
                    );
                  })()}
                  {/* db_write 以外（webhook 等）は従来どおり SQL / ペイロードを表示 */}
                  {s2.sql && s2.node_type !== "sink.db_write" && (
                    <>
                      <div className="wf-preview-label">書き込まれる SQL</div>
                      <pre className="wf-sql">{s2.sql}</pre>
                    </>
                  )}
                  {s2.payload && (
                    <>
                      <div className="wf-preview-label">送信・出力される内容</div>
                      <pre className="wf-sql">{JSON.stringify(s2.payload, null, 2)}</pre>
                    </>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </AppShell>
  );
}

export default function WorkflowEditorPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  return (
    <ReactFlowProvider>
      <WorkflowEditor id={id} />
    </ReactFlowProvider>
  );
}
