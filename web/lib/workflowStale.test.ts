// ワークフロー一覧の「旧版スキーマ」バッジ（lib/workflowStale.ts）の単体テスト。
// 判定はサーバ（GET /workflows の stale_schema_refs）が行うので、ここは表示の導出だけを固定する。

import { describe, expect, it } from "vitest";

import type { StaleSchemaRefDto } from "@/lib/types";

import { STALE_SCHEMA_BADGE, staleSchemaLine, staleSchemaTitle } from "./workflowStale";

function ref(p: Partial<StaleSchemaRefDto> & { node_id: string }): StaleSchemaRefDto {
  return {
    doc_type: "invoice",
    schema_id: "sch_inv_v2",
    schema_version: 2,
    latest_schema_id: "sch_inv_v4",
    latest_version: 4,
    ...p,
  };
}

describe("staleSchemaLine", () => {
  it("ノード・doc_type・参照版 → 最新版を 1 行にする", () => {
    expect(staleSchemaLine(ref({ node_id: "x1" }))).toBe("x1: invoice v2 → 最新 v4");
  });
});

describe("staleSchemaTitle", () => {
  it("旧版参照が無ければ null（バッジを出さない）", () => {
    expect(staleSchemaTitle([])).toBeNull();
    // 旧い gateway は stale_schema_refs を返さない
    expect(staleSchemaTitle(undefined)).toBeNull();
    expect(staleSchemaTitle(null)).toBeNull();
  });

  it("件数・各ノードの内訳・直し方を並べる（サーバの順のまま）", () => {
    const title = staleSchemaTitle([
      ref({ node_id: "x_inv" }),
      ref({
        node_id: "x_rc",
        doc_type: "receipt",
        schema_id: "sch_rc_v1",
        schema_version: 1,
        latest_schema_id: "sch_rc_v3",
        latest_version: 3,
      }),
    ]);
    expect(title).not.toBeNull();
    const lines = (title as string).split("\n");
    expect(lines[0]).toBe("旧版のスキーマを参照している抽出ノードがあります（2 件）");
    expect(lines[1]).toBe("・x_inv: invoice v2 → 最新 v4");
    expect(lines[2]).toBe("・x_rc: receipt v1 → 最新 v3");
    expect(lines[3]).toContain("自動適用されません");
    expect(lines[3]).toContain("帳票種別（常に最新版を使う）");
    expect(lines).toHaveLength(4);
  });

  it("バッジの文言は固定", () => {
    expect(STALE_SCHEMA_BADGE).toBe("⚠ 旧版スキーマ");
  });
});
