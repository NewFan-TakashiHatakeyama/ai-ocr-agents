// ステータスチップ（§2 STATUS CHIPS）の表示名と色。表示の導出だけで DOM・API に触らない。
//
// 語彙は **テーブルごとに別物** なので、表も種類（kind）ごとに分ける
// （docs/design/web-status-labels.md）。以前は documents.status の表 1 つを
// 検証画面の run status・ダッシュボードの Run 内訳・ワークフローの status にも
// 流用していて、表に無い superseded（再抽出で置き換えられた旧 run）や
// draft / active / paused（ワークフロー）が英語の生値のまま灰色で出ていた。
//
// 語彙の出典はマイグレーションの CHECK 制約（db/migrations/versions）。
// statusLabels.test.ts がマイグレーションを読んで、ここの一覧と一致することを確かめる。
// ワークフロー実行（workflow_runs / workflow_node_runs）の表は lib/workflowRuns.ts にある。

/** documents.status（CHECK 制約と 1:1） */
export const DOCUMENT_STATUSES = [
  "uploaded",
  "queued",
  "processing",
  "needs_review",
  "in_review",
  "confirmed",
  "exported",
  "failed",
] as const;

/** extraction_runs.status（CHECK 制約と 1:1）。検証画面の見出しとダッシュボードの Run 内訳 */
export const EXTRACTION_RUN_STATUSES = [
  "processing",
  "needs_review",
  "confirmed",
  "failed",
  "superseded",
] as const;

/** workflows.status（CHECK 制約と 1:1）。retired は CHECK にあるが現状 gateway は書かない */
export const WORKFLOW_STATUSES = ["draft", "active", "paused", "retired"] as const;

export type DocumentStatus = (typeof DOCUMENT_STATUSES)[number];
export type ExtractionRunStatus = (typeof EXTRACTION_RUN_STATUSES)[number];
export type WorkflowStatus = (typeof WORKFLOW_STATUSES)[number];

/** どのテーブルの status か。StatusChip はこれで表を選ぶ */
export type StatusKind = "document" | "extractionRun" | "workflow";

export interface StatusChipView {
  /** チップのクラス（globals.css の .st-*） */
  cls: string;
  label: string;
  /** 補足（チップの title）。ラベルだけでは意味が伝わりにくいものに付ける */
  hint?: string;
}

const DOCUMENT: Record<DocumentStatus, StatusChipView> = {
  uploaded: { cls: "st-uploaded", label: "アップロード済" },
  queued: { cls: "st-queued", label: "処理待ち" },
  processing: { cls: "st-processing", label: "処理中" },
  needs_review: { cls: "st-review", label: "要確認" },
  in_review: { cls: "st-inreview", label: "確認中" },
  confirmed: { cls: "st-confirmed", label: "確定" },
  exported: { cls: "st-exported", label: "連携済" },
  failed: { cls: "st-failed", label: "失敗" },
};

// 共通の 4 値は documents と同じ見た目にする（run の状態は documents に写される）。
// superseded は失敗ではないので赤にせず、「現役でない」灰色の枠線（st-inactive）にする。
const EXTRACTION_RUN: Record<ExtractionRunStatus, StatusChipView> = {
  processing: DOCUMENT.processing,
  needs_review: DOCUMENT.needs_review,
  confirmed: DOCUMENT.confirmed,
  failed: DOCUMENT.failed,
  superseded: {
    cls: "st-inactive",
    label: "再抽出で置き換え済み",
    hint: "再抽出で新しい抽出結果に置き換えられた旧い結果です。この結果は確定できません",
  },
};

// 「有効」「退役」はルール・接続の画面と同じ言葉に揃える。
// paused は止めた操作のボタン名（「停止」）に合わせ、動いていないことが目に付くよう琥珀にする。
const WORKFLOW: Record<WorkflowStatus, StatusChipView> = {
  draft: { cls: "st-uploaded", label: "下書き", hint: "有効にするまで自動実行されません" },
  active: { cls: "st-confirmed", label: "有効" },
  paused: { cls: "st-review", label: "停止中", hint: "停止中は自動実行されません" },
  retired: { cls: "st-inactive", label: "退役" },
};

const TABLES: Record<StatusKind, Readonly<Record<string, StatusChipView>>> = {
  document: DOCUMENT,
  extractionRun: EXTRACTION_RUN,
  workflow: WORKFLOW,
};

function lookup(table: Readonly<Record<string, StatusChipView>>, status: string): StatusChipView | null {
  // Object.hasOwn で引く（"toString" 等の継承プロパティを語彙と取り違えない）
  return Object.hasOwn(table, status) ? table[status] : null;
}

/** 表に無い status か。未知の値は落とさず生値のまま中立色で出す */
export function isKnownStatus(kind: StatusKind, status: string): boolean {
  return lookup(TABLES[kind], status) !== null;
}

/**
 * status → チップの表示名と色。未知の値は生値のまま中立色（gateway が先に語彙を
 * 増やしても画面が壊れないように。workflowRuns の runStatusView と同じ方針）。
 */
export function statusView(kind: StatusKind, status: string): StatusChipView {
  return lookup(TABLES[kind], status) ?? { cls: "st-uploaded", label: status };
}

// ダッシュボードの内訳バーの色。チップの色味に合わせてクラスから引く
const BAR_BY_CLASS: Readonly<Record<string, string>> = {
  "st-processing": "var(--violet)",
  "st-review": "var(--amber)",
  "st-inreview": "var(--blue)",
  "st-confirmed": "var(--green)",
  "st-exported": "var(--blue)",
  "st-failed": "var(--red)",
  "st-inactive": "var(--ink3)",
};

/** 内訳バーの色。対応の無いクラス（uploaded / queued・未知の値）は灰色 */
export function statusBarColor(kind: StatusKind, status: string): string {
  const { cls } = statusView(kind, status);
  return Object.hasOwn(BAR_BY_CLASS, cls) ? BAR_BY_CLASS[cls] : "var(--ink3)";
}
