// 大量処理の純粋関数（設計 bulk-processing §3）。文言と数え方をここで固定する。

import { describe, expect, it } from "vitest";

import type { ExtractBatchResponse } from "@/lib/types";

import {
  classifySkip,
  countRunning,
  partitionFiles,
  partitionSelection,
  summarizeBatch,
  summarizeReasons,
  summarizeUploads,
  type UploadTally,
} from "./bulk";

function res(over: Partial<ExtractBatchResponse>): ExtractBatchResponse {
  return { accepted: [], skipped: [], truncated: false, ...over };
}

function tally(over: Partial<UploadTally>): UploadTally {
  return {
    ok: 0,
    failed: 0,
    failedReasons: [],
    rejected: 0,
    extractStarted: 0,
    extractFailed: 0,
    extractFailedReasons: [],
    ...over,
  };
}

const acc = (id: string) => ({ document_id: id, job_id: `job_${id}`, run_id: `run_${id}` });
const CONFIRMED = {
  document_id: "d_c",
  code: "E1005",
  message: "確定済みの結果があります。再抽出すると確定値が置き換わります",
  reason: "confirmed",
};
const BUSY = {
  document_id: "d_b",
  code: "E1005",
  message: "実行中の Run と競合しています",
  reason: "active_run",
};
const IN_REVIEW = {
  document_id: "d_r",
  code: "E1005",
  message: "確定処理中です。完了してから再抽出してください",
  reason: "in_review",
};
const LOCKED = {
  document_id: "d_l",
  code: "E1005",
  message: "他のユーザーが確認中です",
  reason: "locked",
};
const NO_SCHEMA = {
  document_id: "d_n",
  code: "no_schema",
  message: "種別「receipt」の定義（スキーマ）がありません",
};
const MISSING = { document_id: "d_m", code: "E1001", message: "ドキュメントが見つかりません" };

describe("partitionFiles", () => {
  it("MIME か拡張子で取り込める種類だけ通す", () => {
    const files = [
      { name: "a.pdf", type: "application/pdf" },
      { name: "b.PNG", type: "" }, // MIME 空でも拡張子で通す
      { name: "c.jpeg", type: "image/jpeg" },
      { name: "d.tif", type: "image/tiff" },
      { name: "e.docx", type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document" },
      { name: "f", type: "" },
    ];
    const { accepted, rejected } = partitionFiles(files);
    expect(accepted.map((f) => f.name)).toEqual(["a.pdf", "b.PNG", "c.jpeg", "d.tif"]);
    expect(rejected.map((f) => f.name)).toEqual(["e.docx", "f"]);
  });

  it("空なら両方空", () => {
    expect(partitionFiles([])).toEqual({ accepted: [], rejected: [] });
  });
});

describe("summarizeReasons", () => {
  it("同じ文言はまとめ、空は落とす", () => {
    expect(summarizeReasons(["A", "A", " ", "B"])).toBe("A / B");
    expect(summarizeReasons([])).toBe("");
  });
  it("種類が多いときは先頭 3 種類＋ほか", () => {
    expect(summarizeReasons(["A", "B", "C", "D"])).toBe("A / B / C ほか");
    expect(summarizeReasons(["A", "B", "C"])).toBe("A / B / C");
  });
});

describe("summarizeUploads", () => {
  it("全件成功・抽出開始なし", () => {
    expect(summarizeUploads(tally({ ok: 3 }))).toEqual({
      kind: "ok",
      message: "3 件をアップロードしました。",
    });
  });

  it("抽出も開始した", () => {
    expect(summarizeUploads(tally({ ok: 3, extractStarted: 3 }))).toEqual({
      kind: "ok",
      message: "3 件をアップロードしました。3 件の抽出を開始しました。",
    });
  });

  it("失敗・抽出不可・形式除外は warn で、それぞれ件数と理由を出す", () => {
    const r = summarizeUploads(
      tally({
        ok: 2,
        failed: 1,
        failedReasons: ["サイズ上限 20971520 バイトを超えています"],
        rejected: 2,
        extractStarted: 1,
        extractFailed: 1,
        extractFailedReasons: ["種別「invoice」の定義（スキーマ）がありません"],
      }),
    );
    expect(r.kind).toBe("warn");
    expect(r.message).toBe(
      "2 件をアップロードしました。1 件の抽出を開始しました。" +
        "1 件は抽出を開始できませんでした（種別「invoice」の定義（スキーマ）がありません。帳票ページから開始できます）。" +
        "1 件は失敗しました（サイズ上限 20971520 バイトを超えています）。" +
        "2 件は取り込めない形式（PDF / PNG / JPEG / TIFF 以外）のため除外しました。",
    );
  });

  it("理由が取れなかったときは件数だけ", () => {
    const r = summarizeUploads(tally({ ok: 2, failed: 1, extractFailed: 1 }));
    expect(r.message).toBe(
      "2 件をアップロードしました。" +
        "1 件は抽出を開始できませんでした（帳票ページから開始できます）。" +
        "1 件は失敗しました。",
    );
  });

  it("1 件も上がらなかったときは理由を件数の横に出す（同じ理由はまとめる）", () => {
    expect(
      summarizeUploads(
        tally({ failed: 2, failedReasons: ["非対応または不整合なファイル形式です", "非対応または不整合なファイル形式です"] }),
      ),
    ).toEqual({
      kind: "warn",
      message: "アップロードに失敗しました（2 件: 非対応または不整合なファイル形式です）。",
    });
    expect(summarizeUploads(tally({ failed: 2 }))).toEqual({
      kind: "warn",
      message: "アップロードに失敗しました（2 件）。",
    });
  });
});

describe("classifySkip", () => {
  it("E1005 はサーバの reason で見分ける（確定済み / 処理中 / 確定処理中 / 他者ロック）", () => {
    expect(classifySkip(CONFIRMED)).toBe("confirmed");
    expect(classifySkip(BUSY)).toBe("busy");
    expect(classifySkip({ ...BUSY, reason: "processing" })).toBe("busy");
    expect(classifySkip(IN_REVIEW)).toBe("busy");
    expect(classifySkip(LOCKED)).toBe("locked");
  });
  it("reason の無い応答は文言で確定済みか否かだけ見る（「確定処理中」を確定済みにしない）", () => {
    expect(classifySkip({ ...CONFIRMED, reason: undefined })).toBe("confirmed");
    expect(classifySkip({ ...BUSY, reason: null })).toBe("busy");
    expect(classifySkip({ ...IN_REVIEW, reason: undefined })).toBe("busy");
  });
  it("no_schema / E1001 / その他", () => {
    expect(classifySkip(NO_SCHEMA)).toBe("no_schema");
    expect(classifySkip(MISSING)).toBe("not_found");
    expect(classifySkip({ document_id: "x", code: "E2000", message: "内部エラー" })).toBe("other");
  });
});

describe("summarizeBatch", () => {
  it("全件投入", () => {
    expect(summarizeBatch(res({ accepted: [acc("a"), acc("b")] }))).toEqual({
      kind: "ok",
      message: "2 件を再抽出に投入しました。",
    });
  });

  it("一部スキップは内訳を固定順（確定済み → 処理中 → 他の利用者が確認中 → スキーマなし → 見つからない）で出す", () => {
    const r = summarizeBatch(
      res({
        accepted: [acc("a"), acc("b"), acc("c")],
        skipped: [
          NO_SCHEMA,
          CONFIRMED,
          LOCKED,
          { ...CONFIRMED, document_id: "d_c2" },
          MISSING,
          BUSY,
          IN_REVIEW,
        ],
      }),
    );
    expect(r.kind).toBe("warn");
    expect(r.message).toBe(
      "3 件を再抽出に投入しました（7 件はスキップ: 確定済み 2 / 処理中 2 / 他の利用者が確認中 1 / スキーマなし 1 / 見つからない 1）。",
    );
  });

  it("1 件も投入できなかった", () => {
    expect(summarizeBatch(res({ skipped: [CONFIRMED] }))).toEqual({
      kind: "warn",
      message: "再抽出に投入できませんでした（1 件はスキップ: 確定済み 1）。",
    });
  });

  it("対象なし", () => {
    expect(summarizeBatch(res({}))).toEqual({
      kind: "warn",
      message: "対象の帳票がありませんでした。",
    });
  });

  it("打ち切りは残りがあることを伝える", () => {
    const r = summarizeBatch(res({ accepted: [acc("a")], truncated: true }));
    expect(r.kind).toBe("warn");
    expect(r.message).toBe(
      "1 件を再抽出に投入しました。新しい順に 200 件で打ち切りました。残りはもう一度実行してください。",
    );
  });
});

describe("countRunning / partitionSelection", () => {
  it("queued と processing を数える", () => {
    expect(
      countRunning([
        { status: "queued" },
        { status: "processing" },
        { status: "needs_review" },
        { status: "uploaded" },
      ]),
    ).toBe(2);
    expect(countRunning([])).toBe(0);
  });

  it("選択中の帳票を種別の有無で数える（未選択は無視）", () => {
    const items = [
      { document_id: "a", doc_type: "invoice" },
      { document_id: "b", doc_type: null },
      { document_id: "c" },
      { document_id: "d", doc_type: "invoice" },
    ];
    expect(partitionSelection(items, new Set(["a", "b", "c"]))).toEqual({
      withType: 1,
      withoutType: 2,
    });
    expect(partitionSelection(items, new Set())).toEqual({ withType: 0, withoutType: 0 });
  });
});
