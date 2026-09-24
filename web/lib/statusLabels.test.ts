// ステータスチップの表（lib/statusLabels.ts）と、ワークフロー実行の表（lib/workflowRuns.ts）を
// **サーバの語彙**と突き合わせる。語彙の正本はマイグレーションの CHECK 制約
// （db/migrations/versions）で、DB はこれ以外の値を受け付けない。ここを読んで比べるので、
// マイグレーションで status を足したのに web の表を直し忘れると、このテストが落ちる
// （以前は extraction_runs の superseded が表に無く、英語の生値のまま出ていた）。

import { readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import {
  DOCUMENT_STATUSES,
  EXTRACTION_RUN_STATUSES,
  isKnownStatus,
  type StatusKind,
  statusBarColor,
  statusView,
  WORKFLOW_STATUSES,
} from "./statusLabels";
import { nodeStatusView, runStatusView } from "./workflowRuns";

const here = path.dirname(fileURLToPath(import.meta.url));
const MIGRATIONS = path.resolve(here, "../../db/migrations/versions");
const GLOBALS_CSS = path.resolve(here, "../app/globals.css");

/**
 * マイグレーションの `CHECK (status IN (...))` をテーブルごとに集める。
 * 版番号順に読み、同じテーブルは後の版が勝つ（ALTER TABLE で差し替えた場合）。
 * downgrade() の中は旧い定義に戻す側なので読まない。
 */
function checkedStatuses(): Map<string, string[]> {
  const out = new Map<string, string[]>();
  const files = readdirSync(MIGRATIONS)
    .filter((f) => /^\d+_.+\.py$/.test(f))
    .sort();
  const re =
    /(?:CREATE|ALTER)\s+TABLE\s+(?:IF\s+(?:NOT\s+)?EXISTS\s+)?(\w+)|CHECK\s*\(\s*status\s+IN\s*\(([^)]*)\)/gi;
  for (const f of files) {
    const src = readFileSync(path.join(MIGRATIONS, f), "utf8").split(/^def downgrade/m)[0];
    let table: string | null = null;
    for (const m of src.matchAll(re)) {
      if (m[1]) table = m[1];
      else if (table && m[2] !== undefined) {
        out.set(
          table,
          [...m[2].matchAll(/'([^']+)'/g)].map((v) => v[1]),
        );
      }
    }
  }
  return out;
}

const SERVER = checkedStatuses();

function server(table: string): string[] {
  const v = SERVER.get(table);
  // 読めなければ突き合わせにならない（パーサが壊れた・テーブル名が変わった）ので落とす
  if (!v || v.length === 0) throw new Error(`マイグレーションに ${table}.status の CHECK が見つからない`);
  return v;
}

/** 日本語の表示名が付いているか（生値の英語のまま出ていないか） */
function isJapaneseLabel(label: string, raw: string): boolean {
  return label !== raw && /[^\x00-\x7F]/.test(label);
}

const css = readFileSync(GLOBALS_CSS, "utf8");

function cssDefines(selector: string): boolean {
  const esc = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(`${esc}\\s*[{,]`).test(css);
}

describe("マイグレーションの読み取り", () => {
  it("status の CHECK を持つテーブルを拾えている", () => {
    for (const t of [
      "documents",
      "extraction_runs",
      "workflows",
      "workflow_runs",
      "workflow_node_runs",
    ]) {
      expect(server(t).length).toBeGreaterThan(0);
    }
    // 取り違え防止: review_status の CHECK を status と読んでいない
    expect(server("extraction_runs")).not.toContain("pending");
    expect(server("extraction_runs")).toContain("superseded");
  });
});

describe.each<[StatusKind, string, readonly string[]]>([
  ["document", "documents", DOCUMENT_STATUSES],
  ["extractionRun", "extraction_runs", EXTRACTION_RUN_STATUSES],
  ["workflow", "workflows", WORKFLOW_STATUSES],
])("statusView(%s) と %s.status", (kind, table, list) => {
  it("web の一覧がサーバの CHECK と過不足なく一致する", () => {
    expect([...list].sort()).toEqual([...server(table)].sort());
  });

  it("サーバが返しうる全値に日本語の表示名と定義済みの色がある", () => {
    for (const st of server(table)) {
      const v = statusView(kind, st);
      expect(isKnownStatus(kind, st), `${table}.${st}`).toBe(true);
      expect(isJapaneseLabel(v.label, st), `${table}.${st} → ${v.label}`).toBe(true);
      expect(cssDefines(`.${v.cls}`), `${table}.${st} → .${v.cls}`).toBe(true);
    }
  });

  it("同じ表の中で表示名が重ならない（区別が付く）", () => {
    const labels = server(table).map((st) => statusView(kind, st).label);
    expect(new Set(labels).size).toBe(labels.length);
  });
});

describe("statusView", () => {
  it("再抽出で置き換えられた run は「再抽出で置き換え済み」、失敗とは別の色", () => {
    const v = statusView("extractionRun", "superseded");
    expect(v.label).toBe("再抽出で置き換え済み");
    expect(v.cls).toBe("st-inactive");
    expect(v.cls).not.toBe(statusView("extractionRun", "failed").cls);
    expect(v.hint).toContain("確定できません");
  });

  it("run の共通の値は documents と同じ見た目", () => {
    for (const st of ["processing", "needs_review", "confirmed", "failed"]) {
      expect(statusView("extractionRun", st)).toEqual(statusView("document", st));
    }
  });

  it("ワークフローの status を日本語にする", () => {
    expect(statusView("workflow", "draft").label).toBe("下書き");
    expect(statusView("workflow", "active")).toEqual({ cls: "st-confirmed", label: "有効" });
    expect(statusView("workflow", "paused").label).toBe("停止中");
    expect(statusView("workflow", "retired").label).toBe("退役");
  });

  it("表は種類ごとに別: documents の値を run・ワークフローの表では引かない", () => {
    // uploaded は documents だけの語彙。superseded は run だけの語彙
    expect(isKnownStatus("extractionRun", "uploaded")).toBe(false);
    expect(isKnownStatus("document", "superseded")).toBe(false);
    expect(isKnownStatus("workflow", "confirmed")).toBe(false);
  });

  it("未知の値は生値のまま中立色で出す（落とさない）。継承プロパティも未知扱い", () => {
    expect(statusView("document", "brand_new")).toEqual({ cls: "st-uploaded", label: "brand_new" });
    expect(statusView("workflow", "toString")).toEqual({ cls: "st-uploaded", label: "toString" });
    expect(isKnownStatus("document", "constructor")).toBe(false);
  });
});

describe("statusBarColor", () => {
  it("ダッシュボードの Run 内訳はチップと同じ色味。superseded と未知は灰色", () => {
    expect(statusBarColor("extractionRun", "confirmed")).toBe("var(--green)");
    expect(statusBarColor("extractionRun", "needs_review")).toBe("var(--amber)");
    expect(statusBarColor("extractionRun", "processing")).toBe("var(--violet)");
    expect(statusBarColor("extractionRun", "failed")).toBe("var(--red)");
    expect(statusBarColor("extractionRun", "superseded")).toBe("var(--ink3)");
    expect(statusBarColor("extractionRun", "mystery")).toBe("var(--ink3)");
  });

  it("run の全値に色が決まっている（灰色は superseded だけで、対応漏れの灰色落ちが無い）", () => {
    for (const st of server("extraction_runs")) {
      if (st === "superseded") continue;
      expect(statusBarColor("extractionRun", st), `extraction_runs.${st}`).not.toBe("var(--ink3)");
    }
  });
});

describe("ワークフロー実行（lib/workflowRuns）と workflow_runs / workflow_node_runs.status", () => {
  it("run の全値に日本語の表示名と定義済みの色がある", () => {
    for (const st of server("workflow_runs")) {
      const v = runStatusView(st);
      expect(isJapaneseLabel(v.label, st), `workflow_runs.${st} → ${v.label}`).toBe(true);
      expect(cssDefines(`.chip.wfrun-${v.tone}`), `workflow_runs.${st} → ${v.tone}`).toBe(true);
    }
  });

  it("node_run の全値に日本語の表示名と定義済みの色がある", () => {
    for (const st of server("workflow_node_runs")) {
      const v = nodeStatusView(st);
      expect(isJapaneseLabel(v.label, st), `workflow_node_runs.${st} → ${v.label}`).toBe(true);
      expect(cssDefines(`.chip.wfrun-${v.tone}`), `workflow_node_runs.${st} → ${v.tone}`).toBe(true);
    }
  });
});
