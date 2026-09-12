// PUT /schemas 失敗時のトースト文言（C9-D レビュー修正）。
//
// 固定したいのは 1 点: 新規作成モードの 409 でも details.archived=true なら
// 「同名が既に存在します。既存スキーマを選んで」で潰さず、サーバの文言（復元の案内）を
// 出すこと。アーカイブ済みは既定の一覧に出ていないので、「選んで」は行き止まりになる。

import { describe, expect, it } from "vitest";

import { schemaSaveErrorToast } from "./schemaSaveError";

describe("schemaSaveErrorToast", () => {
  it("新規作成の 409 は既定では『同名が既に存在』", () => {
    const t = schemaSaveErrorToast(
      { status: 409, message: "同名のスキーマが既に存在します。既存スキーマを選んで編集してください" },
      { creating: true, docType: "invoice" },
    );
    expect(t.archived).toBe(false);
    expect(t.message).toContain("既存スキーマを選んで");
  });

  it("新規作成の 409 でも archived=true ならサーバの文言（復元の案内）を出す", () => {
    const t = schemaSaveErrorToast(
      {
        status: 409,
        message:
          "スキーマ「invoice」はアーカイブ済みです。使うには先に復元してください（アーカイブ済みを表示 → 復元）",
        details: { doc_type: "invoice", archived: true },
      },
      { creating: true, docType: "invoice" },
    );
    expect(t.archived).toBe(true);
    expect(t.kind).toBe("warn");
    expect(t.message).toContain("復元");
    expect(t.message).not.toContain("既存スキーマを選んで");
  });

  it("archived=true でサーバ文言が空なら自前の復元案内", () => {
    const t = schemaSaveErrorToast(
      { status: 409, message: "", details: { archived: true } },
      { creating: true, docType: "invoice" },
    );
    expect(t.archived).toBe(true);
    expect(t.message).toContain("invoice");
    expect(t.message).toContain("復元");
  });

  it("編集モードの 409（取得と保存の間にアーカイブされた）も復元の案内", () => {
    const t = schemaSaveErrorToast(
      { status: 409, message: "スキーマ「invoice」はアーカイブ済みです。編集するには先に復元してください", details: { archived: true } },
      { creating: false, docType: "invoice" },
    );
    expect(t.archived).toBe(true);
    expect(t.message).toContain("復元");
  });

  it("409 以外は汎用の失敗文言", () => {
    const t = schemaSaveErrorToast(new Error("boom"), { creating: true, docType: "x" });
    expect(t).toEqual({ kind: "err", archived: false, message: "保存に失敗しました（boom）。" });
  });
});
