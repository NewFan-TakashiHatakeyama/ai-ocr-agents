// 実行履歴タブの純粋関数（lib/workflowRuns.ts）の単体テスト。
// 語彙は runner の射影（running / waiting_hitl / succeeded / failed / skipped）に合わせる。

import { describe, expect, it } from "vitest";

import type { WorkflowNodeRunDto, WorkflowRunItemDto } from "@/lib/types";

import {
  anyRunActive,
  durationMs,
  errorMessage,
  failedNodeOf,
  formatDateTime,
  formatDuration,
  hasRunMovedOn,
  isDocumentDeleted,
  isRunActive,
  nodeStatusView,
  RETRY_BLOCKED_DOCUMENT_DELETED,
  retryBlockedReason,
  runErrorMessage,
  runStatusView,
  triggerLabel,
  waitingLabel,
} from "./workflowRuns";

function node(p: Partial<WorkflowNodeRunDto> & { node_id: string }): WorkflowNodeRunDto {
  return { node_type: "process.extract", status: "succeeded", attempt: 1, ...p };
}

function run(p: Partial<WorkflowRunItemDto> & { status: string }): WorkflowRunItemDto {
  return { id: "wfrun_1", workflow_id: "wf_1", workflow_version: 1, document_id: "doc_1", ...p };
}

describe("runStatusView / nodeStatusView", () => {
  it("run の status を表示名と色に写す", () => {
    expect(runStatusView("running")).toEqual({ label: "実行中", tone: "running" });
    expect(runStatusView("waiting_hitl")).toEqual({ label: "人手確認待ち", tone: "waiting" });
    expect(runStatusView("succeeded")).toEqual({ label: "成功", tone: "ok" });
    expect(runStatusView("failed")).toEqual({ label: "失敗", tone: "failed" });
    expect(runStatusView("skipped")).toEqual({ label: "スキップ", tone: "skipped" });
  });

  it("未知の status は原文のまま中立色で出す（落とさない）", () => {
    expect(runStatusView("waiting_extract")).toEqual({ label: "waiting_extract", tone: "neutral" });
    expect(nodeStatusView("mystery")).toEqual({ label: "mystery", tone: "neutral" });
  });

  it("node の status を写し、待機中の running は「待機中」にする", () => {
    expect(nodeStatusView("pending")).toEqual({ label: "未実行", tone: "neutral" });
    expect(nodeStatusView("running")).toEqual({ label: "実行中", tone: "running" });
    expect(nodeStatusView("running", true)).toEqual({ label: "待機中", tone: "waiting" });
    // 待機ノードでも running でなければそのまま
    expect(nodeStatusView("failed", true)).toEqual({ label: "失敗", tone: "failed" });
  });
});

describe("isRunActive / anyRunActive", () => {
  it("終端（succeeded / failed / skipped）以外は動いている扱い", () => {
    expect(isRunActive("running")).toBe(true);
    expect(isRunActive("waiting_hitl")).toBe(true);
    expect(isRunActive("something_new")).toBe(true);
    expect(isRunActive("succeeded")).toBe(false);
    expect(isRunActive("failed")).toBe(false);
    expect(isRunActive("skipped")).toBe(false);
  });

  it("一覧に 1 件でも動いている run があれば true、空なら false", () => {
    expect(anyRunActive([])).toBe(false);
    expect(anyRunActive([{ status: "failed" }, { status: "succeeded" }])).toBe(false);
    expect(anyRunActive([{ status: "failed" }, { status: "waiting_hitl" }])).toBe(true);
  });
});

describe("waitingLabel", () => {
  it("待機の種類を文にする", () => {
    expect(waitingLabel(null)).toBeNull();
    expect(waitingLabel({})).toBeNull();
    expect(waitingLabel({ kind: "await_extract" })).toBe("抽出の完了を待っています");
    expect(waitingLabel({ kind: "await_hitl" })).toBe("検証画面での確定を待っています");
    expect(waitingLabel({ kind: "await_x" })).toBe("待機中（await_x）");
  });
});

describe("durationMs / formatDuration", () => {
  const T0 = "2026-09-12T10:00:00+09:00";

  it("開始と終了から経過を出す", () => {
    expect(durationMs(T0, "2026-09-12T10:00:03+09:00")).toBe(3000);
  });

  it("終了が無ければ now を終端に、now も無ければ null", () => {
    const now = Date.parse(T0) + 75_000;
    expect(durationMs(T0, null, now)).toBe(75_000);
    expect(durationMs(T0, null)).toBeNull();
  });

  it("開始が無い・読めない日時は null、逆転は 0 に丸める", () => {
    expect(durationMs(null, "2026-09-12T10:00:03+09:00")).toBeNull();
    expect(durationMs("not-a-date", T0)).toBeNull();
    expect(durationMs(T0, "not-a-date")).toBeNull();
    expect(durationMs("2026-09-12T10:00:03+09:00", T0)).toBe(0);
  });

  it("秒・分・時間の単位で読める形にする", () => {
    expect(formatDuration(null)).toBe("—");
    expect(formatDuration(undefined)).toBe("—");
    expect(formatDuration(400)).toBe("1 秒未満");
    expect(formatDuration(3_000)).toBe("3 秒");
    expect(formatDuration(59_999)).toBe("59 秒");
    expect(formatDuration(72_000)).toBe("1 分 12 秒");
    expect(formatDuration(2 * 3600_000 + 5 * 60_000 + 9_000)).toBe("2 時間 5 分");
  });
});

describe("formatDateTime", () => {
  it("無ければ「—」、読めなければ原文", () => {
    expect(formatDateTime(null)).toBe("—");
    expect(formatDateTime(undefined)).toBe("—");
    expect(formatDateTime("garbage")).toBe("garbage");
  });

  it("読める日時は空でない文字列になる", () => {
    expect(formatDateTime("2026-09-12T10:00:00+09:00")).not.toBe("—");
    expect(formatDateTime("2026-09-12T10:00:00+09:00").length).toBeGreaterThan(0);
  });
});

describe("failedNodeOf", () => {
  it("失敗ノードを順序上の先頭で返す。無ければ null", () => {
    expect(failedNodeOf(null)).toBeNull();
    expect(failedNodeOf([])).toBeNull();
    expect(failedNodeOf([node({ node_id: "t1" }), node({ node_id: "x1" })])).toBeNull();
    const runs = [
      node({ node_id: "t1" }),
      node({ node_id: "x1", status: "failed", error: { message: "boom" } }),
      node({ node_id: "s1", status: "failed" }),
    ];
    expect(failedNodeOf(runs)?.node_id).toBe("x1");
  });
});

describe("errorMessage", () => {
  it("message を取り出す。無い形は JSON で見せ、空なら null", () => {
    expect(errorMessage(null)).toBeNull();
    expect(errorMessage(undefined)).toBeNull();
    expect(errorMessage({})).toBeNull();
    expect(errorMessage({ message: "" })).toBeNull();
    expect(errorMessage({ message: "接続に失敗" })).toBe("接続に失敗");
    expect(errorMessage("plain")).toBe("plain");
    expect(errorMessage({ code: "E9" })).toBe('{"code":"E9"}');
  });
});

describe("runErrorMessage", () => {
  const stale = { message: "sink.webhook: 503" };

  it("failed のときだけ run 単位のエラーを出す", () => {
    expect(runErrorMessage(run({ status: "failed", error: stale }))).toBe("sink.webhook: 503");
    expect(runErrorMessage(run({ status: "failed" }))).toBeNull();
    expect(runErrorMessage(null)).toBeNull();
    expect(runErrorMessage(undefined)).toBeNull();
  });

  it("再実行後に成功・待機・実行中になった run に残る前回の error は見せない", () => {
    // runner は成功・待機の射影で error を消さない（COALESCE）ので、旧い失敗文が残る
    expect(runErrorMessage(run({ status: "succeeded", error: stale }))).toBeNull();
    expect(runErrorMessage(run({ status: "waiting_hitl", error: stale }))).toBeNull();
    expect(runErrorMessage(run({ status: "running", error: stale }))).toBeNull();
    expect(runErrorMessage(run({ status: "skipped", error: stale }))).toBeNull();
  });
});

describe("isDocumentDeleted", () => {
  const deletedErr = { code: "E1001", message: "document deleted" };

  it("gateway の旗（document_deleted）で判別する", () => {
    expect(isDocumentDeleted(run({ status: "failed", document_deleted: true, document_id: null }))).toBe(true);
    expect(isDocumentDeleted(run({ status: "failed", document_deleted: false }))).toBe(false);
    expect(isDocumentDeleted(null)).toBe(false);
  });

  it("旗の無い旧 gateway でも、帳票が無く削除時の error（E1001）が付いていれば削除済み扱い", () => {
    expect(isDocumentDeleted(run({ status: "failed", document_id: null, error: deletedErr }))).toBe(true);
    expect(isDocumentDeleted(run({ status: "failed", document_id: undefined, error: deletedErr }))).toBe(true);
  });

  it("帳票が無いだけでは削除済みにしない（schedule 発火の run は最初から帳票を持たない）", () => {
    expect(isDocumentDeleted(run({ status: "failed", document_id: null }))).toBe(false);
    expect(
      isDocumentDeleted(run({ status: "failed", document_id: null, error: { message: "boom" } })),
    ).toBe(false);
    // 帳票が付いたままの E1001 は削除ではない
    expect(isDocumentDeleted(run({ status: "failed", document_id: "doc_1", error: deletedErr }))).toBe(false);
  });
});

describe("triggerLabel", () => {
  it("トリガー種別を日本語にし、未知は原文、無ければ「—」", () => {
    expect(triggerLabel("manual")).toBe("手動実行");
    expect(triggerLabel("schedule")).toBe("スケジュール");
    expect(triggerLabel("s3_event")).toBe("S3 イベント");
    expect(triggerLabel("gdrive_event")).toBe("Google Drive");
    expect(triggerLabel("box_event")).toBe("Box");
    expect(triggerLabel("weird")).toBe("weird");
    expect(triggerLabel(null)).toBe("—");
    expect(triggerLabel(undefined)).toBe("—");
  });
});

describe("retryBlockedReason", () => {
  it("failed だけ再実行できる。他は理由を返す", () => {
    expect(retryBlockedReason(run({ status: "failed" }))).toBeNull();
    expect(retryBlockedReason(run({ status: "running" }))).toContain("実行中");
    expect(retryBlockedReason(run({ status: "waiting_hitl" }))).toContain("人手確認待ち");
    expect(retryBlockedReason(run({ status: "succeeded" }))).toContain("成功");
    expect(retryBlockedReason(run({ status: "skipped" }))).toContain("スキップ");
    expect(retryBlockedReason(run({ status: "unknown" }))).toContain("失敗した実行だけ");
  });

  it("帳票の削除で failed に終端化された run は再実行できない（waiting_hitl が蘇るのを防ぐ）", () => {
    const deleted = run({
      status: "failed",
      document_id: null,
      document_deleted: true,
      error: { code: "E1001", message: "document deleted" },
    });
    expect(retryBlockedReason(deleted)).toBe(RETRY_BLOCKED_DOCUMENT_DELETED);
    expect(retryBlockedReason(deleted)).toContain("帳票が削除された");
    // 旧 gateway（旗なし）でも error から拾う
    expect(
      retryBlockedReason(
        run({ status: "failed", document_id: null, error: { code: "E1001", message: "document deleted" } }),
      ),
    ).toBe(RETRY_BLOCKED_DOCUMENT_DELETED);
  });

  it("帳票を持たない schedule 発火の失敗は再実行できる", () => {
    expect(
      retryBlockedReason(run({ status: "failed", document_id: null, trigger_type: "schedule" })),
    ).toBeNull();
  });

  it("成功した run の帳票が後から削除されても、理由は status のものを優先する", () => {
    expect(
      retryBlockedReason(run({ status: "succeeded", document_id: null, document_deleted: true })),
    ).toContain("成功");
  });
});

describe("hasRunMovedOn", () => {
  const before = { finishedAt: "2026-09-12T10:00:03+09:00" };

  it("status が failed のままで finished_at も同じなら、まだ拾われていない", () => {
    expect(hasRunMovedOn(before, { status: "failed", finished_at: before.finishedAt })).toBe(false);
  });

  it("status が変わった・再び終わった（finished_at が進んだ）・消えた なら動いた扱い", () => {
    expect(hasRunMovedOn(before, { status: "running", finished_at: before.finishedAt })).toBe(true);
    expect(hasRunMovedOn(before, { status: "failed", finished_at: "2026-09-12T10:05:00+09:00" })).toBe(true);
    expect(hasRunMovedOn(before, { status: "failed", finished_at: null })).toBe(true);
    expect(hasRunMovedOn(before, null)).toBe(true);
    expect(hasRunMovedOn({ finishedAt: null }, { status: "failed", finished_at: null })).toBe(false);
  });
});
