// 承認カード（SCR-01, §4.5）の純粋ロジック。
//
// 要点は splitConfirm が confirm_request の残りを **そのまま** params にすること。
// gateway の test_chat_confirm（_web_params）が同じ変換でグラフ→エンドポイントを通して
// いるので、ここが組み替えを始めると両者の契約が静かにずれる。

import { describe, expect, it } from "vitest";

import { deniedMessage, describeConfirm, resultLink, splitConfirm } from "@/lib/chatConfirm";

describe("splitConfirm", () => {
  it("action / prompt を除いた残りを params として返す（rerun_extract）", () => {
    const c = { action: "rerun_extract", document_id: "doc_1", schema_id: "sch_1", prompt: "再抽出しますか？" };
    expect(splitConfirm(c)).toEqual({ action: "rerun_extract", params: { document_id: "doc_1", schema_id: "sch_1" } });
  });

  it("manage_rules は rule_id / status をそのまま送る", () => {
    const c = { action: "manage_rules", rule_id: "rul_1", status: "active", prompt: "有効化しますか？" };
    expect(splitConfirm(c)).toEqual({ action: "manage_rules", params: { rule_id: "rul_1", status: "active" } });
  });

  it("update_schema は従来どおり doc_type / field", () => {
    const field = { name: "payment_method", label: "支払方法", type: "string" };
    const c = { action: "update_schema", doc_type: "invoice", field, prompt: "追加しますか？" };
    expect(splitConfirm(c)).toEqual({ action: "update_schema", params: { doc_type: "invoice", field } });
  });

  it("知らないキーも落とさない（サーバ側の DTO が判定する）", () => {
    const c = { action: "rerun_extract", document_id: "doc_1", supersede_review: false };
    expect(splitConfirm(c).params).toEqual({ document_id: "doc_1", supersede_review: false });
  });

  it("prompt が無くても動く", () => {
    expect(splitConfirm({ action: "manage_rules", rule_id: "r", status: "retired" })).toEqual({
      action: "manage_rules",
      params: { rule_id: "r", status: "retired" },
    });
  });
});

describe("describeConfirm", () => {
  it("rerun_extract: 対象・スキーマ・確認待ちの扱い", () => {
    expect(describeConfirm({ action: "rerun_extract", document_id: "doc_1" })).toEqual([
      "対象: doc_1",
      "スキーマ: 現在の設定",
      "確認待ち（needs_review）の結果は置き換えます",
    ]);
    expect(describeConfirm({ action: "rerun_extract", document_id: "doc_1", schema_id: "sch_2", supersede_review: false })).toEqual([
      "対象: doc_1",
      "スキーマ: sch_2",
      "確認待ちの結果がある場合は実行しません",
    ]);
  });

  it("manage_rules: 操作を日本語に", () => {
    expect(describeConfirm({ action: "manage_rules", rule_id: "rul_1", status: "active" })).toEqual(["ルール: rul_1", "操作: 有効化"]);
    expect(describeConfirm({ action: "manage_rules", rule_id: "rul_1", status: "retired" })).toEqual(["ルール: rul_1", "操作: 退役"]);
    // 未知の語彙は隠さずそのまま出す（サーバが E1003 で断る）
    expect(describeConfirm({ action: "manage_rules", rule_id: "rul_1", status: "rejected" })).toEqual(["ルール: rul_1", "操作: rejected"]);
  });

  it("update_schema: 項目のラベル・名前・型", () => {
    expect(
      describeConfirm({ action: "update_schema", doc_type: "invoice", field: { name: "note", label: "備考", type: "string" } }),
    ).toEqual(["スキーマ: invoice", "項目: 備考（note / string）"]);
    expect(describeConfirm({ action: "update_schema", field: { name: "note" } })).toEqual(["スキーマ: invoice", "項目: note（note）"]);
  });

  it("未知の action は空", () => {
    expect(describeConfirm({ action: "whatever", x: 1 })).toEqual([]);
  });
});

describe("deniedMessage / resultLink", () => {
  it("action ごとの権限文言（gateway の WRITE_TOOL_MIN_ROLE と対応）", () => {
    expect(deniedMessage("update_schema")).toBe("この操作には管理者権限が必要です。");
    expect(deniedMessage("manage_rules")).toBe("この操作には管理者権限が必要です。");
    expect(deniedMessage("rerun_extract")).toBe("この操作にはアップロード以上の権限が必要です。");
    expect(deniedMessage("nope")).toBe("この操作を行う権限がありません。");
  });

  it("成功後の遷移先", () => {
    expect(resultLink("update_schema", {})).toEqual({ href: "/schemas", label: "スキーマを開く" });
    expect(resultLink("manage_rules", {})).toEqual({ href: "/rules", label: "ルールを開く" });
    expect(resultLink("rerun_extract", { document_id: "doc_1", job_id: "j" })).toEqual({
      href: "/documents/doc_1",
      label: "ドキュメントを開く",
    });
    expect(resultLink("rerun_extract", {})).toBeNull();
    expect(resultLink("nope", {})).toBeNull();
  });
});
