// ワークフロー実行履歴（SCR-07「実行」タブ）の純粋関数。表示の導出だけで DOM・API に触らない。
//
// status の語彙は runner の射影（services/orchestrator workflow_runner.py / workflow_graph.py）
// に合わせる。run: running / waiting_hitl / succeeded / failed / skipped、
// node_run: pending / running / succeeded / failed。未知の値は落とさず素通しする
//（gateway が先に語彙を増やしても UI が壊れないように）。

import type { WorkflowNodeRunDto, WorkflowRunError, WorkflowRunItemDto } from "./types";

/** チップの色。CSS は .chip.wfrun-<tone>（globals.css）で定義する */
export type RunTone = "running" | "waiting" | "ok" | "failed" | "skipped" | "neutral";

export interface StatusView {
  label: string;
  tone: RunTone;
}

const RUN_STATUS: Record<string, StatusView> = {
  running: { label: "実行中", tone: "running" },
  waiting_hitl: { label: "人手確認待ち", tone: "waiting" },
  succeeded: { label: "成功", tone: "ok" },
  failed: { label: "失敗", tone: "failed" },
  skipped: { label: "スキップ", tone: "skipped" },
};

const NODE_STATUS: Record<string, StatusView> = {
  pending: { label: "未実行", tone: "neutral" },
  running: { label: "実行中", tone: "running" },
  succeeded: { label: "成功", tone: "ok" },
  failed: { label: "失敗", tone: "failed" },
  skipped: { label: "スキップ", tone: "skipped" },
};

/** run.status → 表示名と色 */
export function runStatusView(status: string): StatusView {
  return RUN_STATUS[status] ?? { label: status, tone: "neutral" };
}

/**
 * node_run.status → 表示名と色。待機中（interrupt）のノードは射影上 running のままなので、
 * run.waiting.node_id と一致する running は「待機中」として見せる。
 */
export function nodeStatusView(status: string, waitingHere = false): StatusView {
  if (waitingHere && status === "running") return { label: "待機中", tone: "waiting" };
  return NODE_STATUS[status] ?? { label: status, tone: "neutral" };
}

/** 終端の status。ここに無いものは「まだ動く」とみなして自動更新を続ける */
const TERMINAL = new Set(["succeeded", "failed", "skipped"]);

export function isRunActive(status: string): boolean {
  return !TERMINAL.has(status);
}

export function anyRunActive(items: readonly { status: string }[]): boolean {
  return items.some((r) => isRunActive(r.status));
}

/** run.waiting.kind → 何を待っているかの文 */
export function waitingLabel(waiting: { kind?: string } | null | undefined): string | null {
  if (!waiting?.kind) return null;
  if (waiting.kind === "await_extract") return "抽出の完了を待っています";
  if (waiting.kind === "await_hitl") return "検証画面での確定を待っています";
  return `待機中（${waiting.kind}）`;
}

/**
 * 経過ミリ秒。finished が無ければ now を終端にする（実行中の経過表示）。
 * 開始が無い・日時が読めない・now も無い場合は null。
 */
export function durationMs(
  startedAt: string | null | undefined,
  finishedAt: string | null | undefined,
  now?: number,
): number | null {
  if (!startedAt) return null;
  const start = Date.parse(startedAt);
  if (Number.isNaN(start)) return null;
  let end: number;
  if (finishedAt) {
    end = Date.parse(finishedAt);
    if (Number.isNaN(end)) return null;
  } else if (now !== undefined) {
    end = now;
  } else {
    return null;
  }
  return Math.max(0, end - start);
}

/** ミリ秒 → 「3 秒」「1 分 12 秒」「2 時間 5 分」。null は「—」 */
export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || Number.isNaN(ms)) return "—";
  const sec = Math.floor(ms / 1000);
  if (sec < 1) return "1 秒未満";
  if (sec < 60) return `${sec} 秒`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min} 分 ${sec % 60} 秒`;
  const hour = Math.floor(min / 60);
  return `${hour} 時間 ${min % 60} 分`;
}

/** ISO 日時 → ja-JP の短い表記。無ければ「—」、読めなければ原文 */
export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso;
  return new Date(t).toLocaleString("ja-JP", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

/** 失敗したノード（順序上の先頭）。無ければ null */
export function failedNodeOf(
  nodeRuns: readonly WorkflowNodeRunDto[] | null | undefined,
): WorkflowNodeRunDto | null {
  return nodeRuns?.find((n) => n.status === "failed") ?? null;
}

/**
 * run 単位のエラー文。failed のときだけ。
 *
 * runner は成功・待機の射影で error を消さない（workflow_store の
 * `error = COALESCE(:err, error)`）ので、再実行して成功した run にも前回の失敗文が
 * 残っている。成功チップの下に赤いエラーを出すと「結局失敗したのか」が読めなくなるため、
 * 一覧・ドロワーとも status が failed のときに限って見せる。
 */
export function runErrorMessage(
  run: Pick<WorkflowRunItemDto, "status" | "error"> | null | undefined,
): string | null {
  if (!run || run.status !== "failed") return null;
  return errorMessage(run.error);
}

/** 射影の error → 表示文。{message} が無い形でも捨てずに JSON で見せる */
export function errorMessage(err: WorkflowRunError | string | null | undefined): string | null {
  if (err === null || err === undefined) return null;
  if (typeof err === "string") return err || null;
  const m = err.message;
  if (typeof m === "string" && m.trim() !== "") return m;
  const rest = Object.keys(err).filter((k) => k !== "message");
  if (rest.length === 0) return null;
  try {
    return JSON.stringify(err);
  } catch {
    return String(err);
  }
}

const TRIGGER_LABEL: Record<string, string> = {
  manual: "手動実行",
  schedule: "スケジュール",
  s3_event: "S3 イベント",
  gdrive_event: "Google Drive",
  m365_event: "Microsoft 365",
  box_event: "Box",
  email_attachment: "メール添付",
};

/** trigger.type → 表示名。無ければ「—」（旧 run / 旧 gateway）、未知は原文 */
export function triggerLabel(type: string | null | undefined): string {
  if (!type) return "—";
  return TRIGGER_LABEL[type] ?? type;
}

/**
 * 帳票の削除で切り離された run か。gateway の旗（document_deleted）を見る。
 * 旗を出さない旧 gateway 向けに、削除時に書かれる error（code E1001「document deleted」）
 * も拾う。document_id が無いだけでは判別しない（schedule 発火の run は最初から帳票が無い）。
 */
export function isDocumentDeleted(
  run: Pick<WorkflowRunItemDto, "document_id" | "document_deleted" | "error"> | null | undefined,
): boolean {
  if (!run) return false;
  if (run.document_deleted === true) return true;
  return (
    (run.document_id === null || run.document_id === undefined) &&
    typeof run.error === "object" &&
    run.error !== null &&
    run.error.code === "E1001"
  );
}

export const RETRY_BLOCKED_DOCUMENT_DELETED = "帳票が削除されたため再実行できません";

/**
 * 再実行できない理由（title 用）。failed だけが再実行可能（gateway は他を 409 で弾く）。
 * ただし帳票の削除で failed に終端化された run は除く: retry すると checkpoint の
 * interrupt が再び立って、帳票の無い waiting_hitl が蘇る（確定する画面も止める API も無い）。
 * 可能なら null。
 */
export function retryBlockedReason(
  run: Pick<WorkflowRunItemDto, "status" | "document_id" | "document_deleted" | "error">,
): string | null {
  switch (run.status) {
    case "failed":
      return isDocumentDeleted(run) ? RETRY_BLOCKED_DOCUMENT_DELETED : null;
    case "running":
      return "実行中のため再実行できません";
    case "waiting_hitl":
      return "人手確認待ちのため再実行できません（検証画面で確定すると続きが走ります）";
    case "succeeded":
      return "成功した実行は再実行できません";
    case "skipped":
      return "スキップされた実行は再実行できません";
    default:
      return "失敗した実行だけ再実行できます";
  }
}

/**
 * 再実行を投げたあと、その run が動き出した（または再び失敗して終わった）か。
 * worker が拾うまで status は failed のままなので、status が変わるか finished_at が
 * 進むかで判定する。run が見つからなければ「動いた」扱いで監視を外す。
 */
export function hasRunMovedOn(
  before: { finishedAt: string | null },
  after: Pick<WorkflowRunItemDto, "status" | "finished_at"> | null | undefined,
): boolean {
  if (!after) return true;
  if (after.status !== "failed") return true;
  return (after.finished_at ?? null) !== before.finishedAt;
}
