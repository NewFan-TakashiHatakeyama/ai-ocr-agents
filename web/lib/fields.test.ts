// lib/fields の純粋関数。vChecks はサーバ（orchestrator の validate ノード）が返す
// validation の形に合わせて固定する: checks は {check, passed, severity} のオブジェクト配列。
// 以前は string[] として扱っていて、全チェック合格の項目があると検証画面が落ちていた。

import { describe, expect, it } from "vitest";

import { vChecks } from "@/lib/fields";
import type { ExtractedField } from "@/lib/types";

function field(validation: ExtractedField["validation"]): ExtractedField {
  return {
    name: "invoice_date",
    value_raw: "令和02年01月31日",
    value_normalized: "2020-01-31",
    confidence: 0.85,
    grounding_score: 0.85,
    validation,
    review_status: "auto",
  } as ExtractedField;
}

describe("vChecks", () => {
  it("サーバの形（{check, passed, severity}）から合格したチェックの ID を返す", () => {
    // dev DB の実データ（sample2 を invoice v7 で抽出した run の invoice_date）と同じ形
    const f = field({ checks: [{ check: "V-DATE", passed: true, severity: "info" }], passed: true });
    expect(vChecks(f)).toEqual(["V-DATE"]);
  });

  it("項目全体が不合格ならバッジを出さない（合格の表示なので）", () => {
    const f = field({ checks: [{ check: "V-REGNO", passed: false, severity: "error" }], passed: false });
    expect(vChecks(f)).toEqual([]);
  });

  it("旧形（文字列の配列）も ID として受ける", () => {
    expect(vChecks(field({ checks: ["V-SUM", "V-SUM"], passed: true }))).toEqual(["V-SUM"]);
  });

  it("validation が無い・checks が配列でない・壊れた要素は無視する", () => {
    expect(vChecks(field(null))).toEqual([]);
    expect(vChecks(field(undefined))).toEqual([]);
    expect(vChecks(field({ passed: true }))).toEqual([]);
    const broken = { checks: [{ passed: true } as unknown as string, "", { check: "V-DATE", passed: true }], passed: true };
    expect(vChecks(field(broken))).toEqual(["V-DATE"]);
  });

  it("返す要素はすべて文字列（React の子に渡せる）", () => {
    const f = field({
      checks: [
        { check: "V-DATE", passed: true, severity: "info" },
        { check: "V-SUM", passed: true, severity: "warning" },
      ],
      passed: true,
    });
    expect(vChecks(f).every((c) => typeof c === "string")).toBe(true);
  });
});
