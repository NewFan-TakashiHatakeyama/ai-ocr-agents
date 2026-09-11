// TemplatizePreview から切り出した純粋関数の単体テスト（設計 v2 §1.4・§6）。
//
// ここで固定するのは「切り出し前の save() / issue() / ghosts と同じ出力」であって、
// 望ましい振る舞いの提案ではない。文言・分岐・順序は現状のまま写している。

import { describe, expect, it } from "vitest";

import type { ExtractedField, PageDim, RegionRect, SchemaFieldDto } from "@/lib/types";

import type { RunSpanDto, RunSpans } from "@/lib/types";

import {
  buildSaveBody,
  denormalize,
  EXAMPLE_VALUE_MAX_LEN,
  exampleValueFromQuote,
  exampleValueFromSpans,
  GHOST_QUOTE_MAX_LEN,
  normalize,
  resolveGhosts,
  spansInRect,
  truncateChars,
  validateDrafts,
  type DraftRow,
  type Preserved,
  type PreviewRegion,
  type Px,
} from "./templatize";

// ---- フィクスチャ ----

const DIMS: PageDim[] = [
  { page_no: 1, width: 1000, height: 1000 },
  { page_no: 2, width: 1000, height: 1000 },
  { page_no: 3, width: null, height: null }, // 寸法が無いページ
];

const NO_PRESERVED: Preserved = { fieldRegions: {}, excludes: [], sourcePageCount: null };

function row(p: Partial<DraftRow> & { rowId: string }): DraftRow {
  return { name: p.rowId, label: p.rowId, type: "string", include: true, sample: "", ...p };
}

function baseField(p: Partial<SchemaFieldDto> & { name: string }): SchemaFieldDto {
  return { label: p.name, type: "string", required: false, critical: false, ...p };
}

function field(p: Partial<ExtractedField> & { name: string }): ExtractedField {
  return {
    span_ids: [],
    confidence: 0.9,
    grounding_score: 0.9,
    review_status: "auto",
    ...p,
  };
}

/** 読み込み時と同じ手順で「触っていない」領域を作る（origin と originBbox を持つ）。 */
function loadedRegion(
  origin: RegionRect,
  p: Partial<PreviewRegion> & { id: string; kind: "include" | "exclude" },
): PreviewRegion {
  const drawnPage = typeof origin.page === "number" ? origin.page : 1;
  const px = denormalize(origin.rect, 1000, 1000);
  return {
    drawnPage,
    page: p.kind === "include" ? drawnPage : (origin.page ?? null),
    bbox: px,
    origin,
    originBbox: px,
    label: p.kind === "exclude" ? (origin.label ?? undefined) : undefined,
    ...p,
  };
}

function build(over: Partial<Parameters<typeof buildSaveBody>[0]>) {
  return buildSaveBody({
    drafts: [],
    regions: [],
    preserved: NO_PRESERVED,
    dimsUnavailable: false,
    pageCount: 3,
    pageDims: DIMS,
    mode: "edit",
    ...over,
  });
}

// ---- buildSaveBody ----

describe("buildSaveBody: 読取領域", () => {
  it("既存行の矩形を触っていなければ origin をそのまま返す（丸め往復で座標が動かない）", () => {
    // 1000px に対して 0.12345 は 123px に丸まり、正規化し直すと 0.123 になる。
    // 触っていない矩形は原本を返すので、この誤差が乗ってはいけない。
    const origin: RegionRect = { page: 2, rect: [0.12345, 0.2, 0.34567, 0.4] };
    expect(normalize(denormalize(origin.rect, 1000, 1000), 1000, 1000)).not.toEqual(origin.rect);

    const base = baseField({
      name: "total",
      label: "合計",
      type: "money_jpy",
      required: true,
      critical: true,
      region: origin,
    });
    const drafts = [row({ rowId: "r1", name: "total", label: "合計金額", type: "money_jpy", base })];
    const regions = [loadedRegion(origin, { id: "g1", kind: "include", rowId: "r1" })];

    const body = build({ drafts, regions });
    expect(body.fields).toHaveLength(1);
    expect(body.fields[0].region).toBe(origin);
    // base は丸ごと保全し、name / label / type だけ画面の値で上書きする
    expect(body.fields[0]).toEqual({
      ...base,
      name: "total",
      label: "合計金額",
      type: "money_jpy",
      region: origin,
    });
  });

  it("base の未知キーも保全する（put_schema は全置換）", () => {
    const base = {
      ...baseField({ name: "items", type: "table" }),
      columns: [{ name: "qty", type: "number" }],
      extra_from_server: "keep",
    } as SchemaFieldDto;
    const body = build({ drafts: [row({ rowId: "r1", name: "items", type: "table", base })] });
    expect(body.fields[0]).toMatchObject({
      columns: [{ name: "qty", type: "number" }],
      extra_from_server: "keep",
      region: null,
    });
  });

  it("矩形を引き直した既存行は drawnPage の寸法で正規化する", () => {
    const origin: RegionRect = { page: 1, rect: [0.1, 0.1, 0.2, 0.2], label: "旧" };
    const base = baseField({ name: "date", region: origin });
    const r = loadedRegion(origin, { id: "g1", kind: "include", rowId: "r1" });
    const moved: PreviewRegion = { ...r, bbox: [100, 200, 300, 400] as Px };

    const body = build({ drafts: [row({ rowId: "r1", name: "date", base })], regions: [moved] });
    expect(body.fields[0].region).toEqual({ page: 1, rect: [0.1, 0.2, 0.3, 0.4] });
  });

  it("新しく引いた領域（origin 無し）は正規化して返す", () => {
    const regions: PreviewRegion[] = [
      { id: "g1", kind: "include", bbox: [250, 500, 750, 1000], drawnPage: 2, page: 2, rowId: "r1" },
    ];
    const body = build({ drafts: [row({ rowId: "r1", name: "amount" })], regions });
    expect(body.fields[0]).toEqual({
      name: "amount",
      label: "r1",
      type: "string",
      required: false,
      critical: false,
      region: { page: 2, rect: [0.25, 0.5, 0.75, 1] },
    });
  });

  it("正規化は [0,1] に丸める（画像外にはみ出した矩形）", () => {
    const regions: PreviewRegion[] = [
      { id: "g1", kind: "include", bbox: [-10, 0, 1200, 500], drawnPage: 1, page: 1, rowId: "r1" },
    ];
    const body = build({ drafts: [row({ rowId: "r1" })], regions });
    expect(body.fields[0].region).toEqual({ page: 1, rect: [0, 0, 1, 0.5] });
  });

  it("寸法の無いページに引いた領域は preserved の値に戻す（無ければ null）", () => {
    const kept: RegionRect = { page: 3, rect: [0.1, 0.1, 0.2, 0.2] };
    const regions: PreviewRegion[] = [
      { id: "g1", kind: "include", bbox: [1, 2, 3, 4], drawnPage: 3, page: 3, rowId: "r1" },
      { id: "g2", kind: "include", bbox: [1, 2, 3, 4], drawnPage: 3, page: 3, rowId: "r2" },
    ];
    const body = build({
      drafts: [row({ rowId: "r1" }), row({ rowId: "r2" })],
      regions,
      preserved: { ...NO_PRESERVED, fieldRegions: { r1: kept } },
    });
    expect(body.fields[0].region).toBe(kept);
    expect(body.fields[1].region).toBeNull();
  });

  it("画面に領域が無い行は preserved.fieldRegions を無変換で返す", () => {
    const kept: RegionRect = { page: "last", rect: [0.5, 0.5, 0.6, 0.6] };
    const body = build({
      drafts: [row({ rowId: "r1", base: baseField({ name: "r1", region: kept }) })],
      preserved: { ...NO_PRESERVED, fieldRegions: { r1: kept } },
    });
    expect(body.fields[0].region).toBe(kept);
  });
});

describe("buildSaveBody: 行の取捨", () => {
  it("既存行（base あり）は include=false でも版から落とさない。region は null", () => {
    const kept: RegionRect = { page: 1, rect: [0.1, 0.1, 0.2, 0.2] };
    const base = baseField({ name: "memo", required: true, region: kept });
    const body = build({
      drafts: [row({ rowId: "r1", name: "memo", include: false, base })],
      // 画面上にも preserved にも領域があるが、include=false なら付けない
      regions: [loadedRegion(kept, { id: "g1", kind: "include", rowId: "r1" })],
      preserved: { ...NO_PRESERVED, fieldRegions: { r1: kept } },
    });
    expect(body.fields).toEqual([{ ...base, name: "memo", label: "r1", type: "string", region: null }]);
  });

  it("base 無しの行は include=false なら落ちる", () => {
    const body = build({
      drafts: [
        row({ rowId: "r1", name: "drop_me", include: false }),
        row({ rowId: "r2", name: "keep_me", include: true }),
      ],
    });
    expect(body.fields.map((f) => f.name)).toEqual(["keep_me"]);
    expect(body.fields[0]).toEqual({
      name: "keep_me",
      label: "r2",
      type: "string",
      required: false,
      critical: false,
      region: null,
    });
  });

  it("name / label は trim し、label が空なら name で埋める", () => {
    const body = build({
      drafts: [
        row({ rowId: "r1", name: "  total ", label: "  " }),
        row({ rowId: "r2", name: "date", label: " 日付 " }),
      ],
    });
    expect(body.fields.map((f) => [f.name, f.label])).toEqual([
      ["total", "total"],
      ["date", "日付"],
    ]);
  });

  it("行の順序を保つ", () => {
    const body = build({
      drafts: [row({ rowId: "b" }), row({ rowId: "a" }), row({ rowId: "c" })],
    });
    expect(body.fields.map((f) => f.name)).toEqual(["b", "a", "c"]);
  });
});

describe("buildSaveBody: 除外領域", () => {
  const preservedEx: RegionRect = { page: 3, rect: [0.9, 0.9, 1, 1], label: "寸法なしページ" };
  const untouchedOrigin: RegionRect = { page: null, rect: [0.11111, 0.2, 0.3, 0.4], label: "社印" };

  it("preserved.excludes が先頭にそのまま入り、新しく引いた除外は正規化されて続く", () => {
    const untouched = loadedRegion(untouchedOrigin, { id: "e1", kind: "exclude" });
    const drawn: PreviewRegion = {
      id: "e2",
      kind: "exclude",
      bbox: [100, 200, 300, 400],
      drawnPage: 1,
      page: 1,
      label: "  ロゴ ",
    };
    const noLabel: PreviewRegion = {
      id: "e3",
      kind: "exclude",
      bbox: [0, 0, 500, 500],
      drawnPage: 2,
      page: "last",
      label: "",
    };
    const body = build({
      regions: [drawn, untouched, noLabel],
      preserved: { ...NO_PRESERVED, excludes: [preservedEx] },
    });
    expect(body.excludeRegions).toEqual([
      preservedEx,
      { page: 1, rect: [0.1, 0.2, 0.3, 0.4], label: "ロゴ" },
      untouchedOrigin,
      { page: "last", rect: [0, 0, 0.5, 0.5], label: null },
    ]);
    expect(body.excludeRegions[0]).toBe(preservedEx);
    expect(body.excludeRegions[2]).toBe(untouchedOrigin);
  });

  it("矩形は同じでも名前か適用範囲を変えた除外は正規化し直す", () => {
    const base = loadedRegion(untouchedOrigin, { id: "e1", kind: "exclude" });
    const renamed: PreviewRegion = { ...base, id: "e1", label: "角印" };
    const rescoped: PreviewRegion = { ...base, id: "e2", page: 1 };
    const body = build({ regions: [renamed, rescoped] });
    expect(body.excludeRegions).toEqual([
      { page: null, rect: [0.111, 0.2, 0.3, 0.4], label: "角印" },
      { page: 1, rect: [0.111, 0.2, 0.3, 0.4], label: "社印" },
    ]);
  });

  it("除外領域が無ければ preserved.excludes だけを返す（空なら空配列）", () => {
    expect(build({}).excludeRegions).toEqual([]);
    expect(
      build({ preserved: { ...NO_PRESERVED, excludes: [preservedEx] } }).excludeRegions,
    ).toEqual([preservedEx]);
  });

  it("寸法の無いページに新しく引いた除外は落とす", () => {
    const body = build({
      regions: [{ id: "e1", kind: "exclude", bbox: [1, 2, 3, 4], drawnPage: 3, page: 3 }],
    });
    expect(body.excludeRegions).toEqual([]);
  });

  it("include 領域は除外領域に混ざらない", () => {
    const body = build({
      drafts: [row({ rowId: "r1" })],
      regions: [{ id: "g1", kind: "include", bbox: [0, 0, 100, 100], drawnPage: 1, page: 1, rowId: "r1" }],
    });
    expect(body.excludeRegions).toEqual([]);
  });
});

describe("buildSaveBody: sourcePageCount", () => {
  it("dimsUnavailable のとき create では undefined（キーを送らない）", () => {
    const body = build({ mode: "create", dimsUnavailable: true, pageDims: [], pageCount: 1 });
    expect(body.sourcePageCount).toBeUndefined();
  });

  it("dimsUnavailable のとき edit では preserved の値をそのまま（pageCount で埋めない）", () => {
    expect(
      build({
        dimsUnavailable: true,
        pageDims: [],
        pageCount: 1,
        preserved: { ...NO_PRESERVED, sourcePageCount: 4 },
      }).sourcePageCount,
    ).toBe(4);
    expect(
      build({ dimsUnavailable: true, pageDims: [], pageCount: 1 }).sourcePageCount,
    ).toBeNull();
  });

  it("寸法があるとき create は今回のページ数、edit は既存値（未記録なら今回の値）", () => {
    expect(build({ mode: "create", pageCount: 3 }).sourcePageCount).toBe(3);
    expect(
      build({ mode: "edit", pageCount: 3, preserved: { ...NO_PRESERVED, sourcePageCount: 2 } })
        .sourcePageCount,
    ).toBe(2);
    expect(build({ mode: "edit", pageCount: 3 }).sourcePageCount).toBe(3);
  });
});

describe("buildSaveBody: 入力を壊さない", () => {
  it("drafts / regions / preserved を変更しない", () => {
    const origin: RegionRect = { page: 1, rect: [0.1, 0.1, 0.2, 0.2] };
    const drafts = [row({ rowId: "r1", base: baseField({ name: "r1", region: origin }) })];
    const regions = [loadedRegion(origin, { id: "g1", kind: "include", rowId: "r1" })];
    const preserved: Preserved = { fieldRegions: {}, excludes: [origin], sourcePageCount: 1 };
    const snapshot = JSON.stringify({ drafts, regions, preserved });
    build({ drafts, regions, preserved });
    expect(JSON.stringify({ drafts, regions, preserved })).toBe(snapshot);
  });
});

// ---- validateDrafts ----

describe("validateDrafts", () => {
  const ok = [row({ rowId: "r1", name: "total" })];

  it("問題が無ければ null", () => {
    expect(validateDrafts({ docType: "invoice", drafts: ok, includes: [] })).toBeNull();
  });

  it("doc_type が空（空白のみ）", () => {
    const msg = "帳票種別（doc_type）を入力してください。";
    expect(validateDrafts({ docType: "", drafts: ok, includes: [] })).toBe(msg);
    expect(validateDrafts({ docType: "   ", drafts: ok, includes: [] })).toBe(msg);
  });

  it("残す項目が 0", () => {
    const msg = "抽出する項目を 1 つ以上選んでください。";
    expect(validateDrafts({ docType: "invoice", drafts: [], includes: [] })).toBe(msg);
    expect(
      validateDrafts({
        docType: "invoice",
        drafts: [row({ rowId: "r1", include: false })],
        includes: [],
      }),
    ).toBe(msg);
  });

  describe("命名規則は新しく付けた名前だけに課す", () => {
    const msg = "項目名（name）は英字始まりの英数字・アンダースコアにしてください。";

    it("新規行の日本語名は弾く", () => {
      expect(
        validateDrafts({ docType: "x", drafts: [row({ rowId: "r1", name: "請求日" })], includes: [] }),
      ).toBe(msg);
    });

    it("数字始まり・記号入り・前後空白のみは弾く", () => {
      for (const name of ["1st", "_x", "a-b", "a b", ""]) {
        expect(
          validateDrafts({ docType: "x", drafts: [row({ rowId: "r1", name })], includes: [] }),
        ).toBe(msg);
      }
    });

    it("base と同名なら日本語でも通る（開いて保存するだけができる）", () => {
      const drafts = [row({ rowId: "r1", name: "請求日", base: baseField({ name: "請求日" }) })];
      expect(validateDrafts({ docType: "x", drafts, includes: [] })).toBeNull();
    });

    it("base があっても名前を変えたら規則が課される", () => {
      const drafts = [row({ rowId: "r1", name: "支払日", base: baseField({ name: "請求日" }) })];
      expect(validateDrafts({ docType: "x", drafts, includes: [] })).toBe(msg);
    });

    it("include=false の行は見ない", () => {
      const drafts = [row({ rowId: "r1", name: "請求日", include: false }), ...ok];
      expect(validateDrafts({ docType: "x", drafts, includes: [] })).toBeNull();
    });

    it("前後の空白は trim してから判定する", () => {
      const drafts = [row({ rowId: "r1", name: "  total  " })];
      expect(validateDrafts({ docType: "x", drafts, includes: [] })).toBeNull();
    });
  });

  it("項目名の重複（trim 後で比較）", () => {
    const drafts = [row({ rowId: "r1", name: "total" }), row({ rowId: "r2", name: " total " })];
    expect(validateDrafts({ docType: "x", drafts, includes: [] })).toBe(
      "項目名（name）が重複しています。",
    );
  });

  it("項目に紐づいていない読取領域（行が無い／include=false）", () => {
    const msg = "項目に紐づいていない読取領域があります。項目を選び直してください。";
    const region = (rowId: string): PreviewRegion => ({
      id: `g-${rowId}`,
      kind: "include",
      bbox: [0, 0, 1, 1],
      drawnPage: 1,
      page: 1,
      rowId,
    });
    expect(validateDrafts({ docType: "x", drafts: ok, includes: [region("gone")] })).toBe(msg);
    const drafts = [...ok, row({ rowId: "r2", include: false })];
    expect(validateDrafts({ docType: "x", drafts, includes: [region("r2")] })).toBe(msg);
    expect(validateDrafts({ docType: "x", drafts, includes: [region("r1")] })).toBeNull();
  });

  it("検査の順序: doc_type → 項目数 → 命名規則 → 重複 → 孤立領域", () => {
    const bad = [row({ rowId: "r1", name: "請求日" }), row({ rowId: "r2", name: "請求日" })];
    expect(validateDrafts({ docType: "", drafts: bad, includes: [] })).toBe(
      "帳票種別（doc_type）を入力してください。",
    );
    expect(validateDrafts({ docType: "x", drafts: bad, includes: [] })).toBe(
      "項目名（name）は英字始まりの英数字・アンダースコアにしてください。",
    );
  });
});

// ---- resolveGhosts ----

describe("resolveGhosts", () => {
  const bbox: Px = [10, 20, 30, 40];
  const f = field({ name: "total", label: "合計", page: 1, bbox });
  const drafts = [row({ rowId: "r1", name: "total" })];

  it("未確定の行には出る（label が無ければ name）", () => {
    const fields = [f, field({ name: "date", page: 1, bbox })];
    const ds = [...drafts, row({ rowId: "r2", name: "date" })];
    expect(resolveGhosts({ fields, drafts: ds, regionByRow: new Map(), page: 1, mode: "create" })).toEqual([
      { key: "total", bbox, label: "合計" },
      { key: "date", bbox, label: "date" },
    ]);
  });

  it("確定済み（領域を持つ）行には出ない", () => {
    const regionByRow = new Map<string, PreviewRegion>([
      ["r1", { id: "g1", kind: "include", bbox, drawnPage: 1, page: 1, rowId: "r1" }],
    ]);
    expect(resolveGhosts({ fields: [f], drafts, regionByRow, page: 1, mode: "create" })).toEqual([]);
  });

  it("別ページには出ない（page 未指定は 1 ページ目扱い）", () => {
    const noPage = field({ name: "total", bbox, page: null });
    expect(resolveGhosts({ fields: [f], drafts, regionByRow: new Map(), page: 2, mode: "create" })).toEqual([]);
    expect(
      resolveGhosts({ fields: [noPage], drafts, regionByRow: new Map(), page: 1, mode: "create" }),
    ).toHaveLength(1);
    expect(
      resolveGhosts({ fields: [noPage], drafts, regionByRow: new Map(), page: 2, mode: "create" }),
    ).toEqual([]);
  });

  it("bbox の無い項目は出ない", () => {
    const fields = [field({ name: "total", page: 1, bbox: null }), field({ name: "total", page: 1 })];
    expect(resolveGhosts({ fields, drafts, regionByRow: new Map(), page: 1, mode: "create" })).toEqual([]);
  });

  it("一覧に無い名前の項目は出ない", () => {
    expect(
      resolveGhosts({ fields: [f], drafts: [row({ rowId: "r9", name: "other" })], regionByRow: new Map(), page: 1, mode: "create" }),
    ).toEqual([]);
  });

  it("create 以外で drafts が空なら出ない（編集モードの読み込み中）", () => {
    expect(resolveGhosts({ fields: [f], drafts: [], regionByRow: new Map(), page: 1, mode: "edit" })).toEqual([]);
    // 編集モードでも行が揃えば出る
    expect(resolveGhosts({ fields: [f], drafts, regionByRow: new Map(), page: 1, mode: "edit" })).toHaveLength(1);
  });
});

// =====================================================================
// 設計 region-field-add-and-hint-v2 Part 1（コミット 3）で足した振る舞い。
// 上の既存テストは「切り出し前と同じ出力」の固定なので触らない。
// =====================================================================

/** この画面で足した新規行（base 無し・isNew）。 */
function newRow(p: Partial<DraftRow> & { rowId: string }): DraftRow {
  return { name: "", label: "", type: "string", include: true, sample: "", isNew: true, ...p };
}

/** この画面で作った include 領域（出どころ付き）。 */
function drawn(
  p: Partial<PreviewRegion> & { id: string; rowId: string },
): PreviewRegion {
  return {
    kind: "include",
    bbox: [250, 500, 750, 1000],
    drawnPage: 2,
    page: 2,
    originKind: "manual",
    createdAt: "2026-09-12T00:00:00.000Z",
    exampleValue: "株式会社サンプル",
    ...p,
  };
}

// ---- buildSaveBody: 新規行と出どころ（D1・D8・D11） ----

describe("buildSaveBody: 新規行（isNew）", () => {
  it("base 無しの分岐に落ち、region に example_value / origin / created_at が載る", () => {
    const drafts = [newRow({ rowId: "n1", name: "due_date", label: "支払期日", type: "date" })];
    const regions = [drawn({ id: "g1", rowId: "n1" })];
    const body = build({ drafts, regions, mode: "create" });
    expect(body.fields).toEqual([
      {
        name: "due_date",
        label: "支払期日",
        type: "date",
        required: false,
        critical: false,
        region: {
          page: 2,
          rect: [0.25, 0.5, 0.75, 1],
          example_value: "株式会社サンプル",
          origin: "manual",
          created_at: "2026-09-12T00:00:00.000Z",
        },
      },
    ]);
  });

  it("編集モードでも同じ分岐（既存行の後ろに新しい項目として並ぶ）", () => {
    const origin: RegionRect = { page: 1, rect: [0.1, 0.1, 0.2, 0.2], example_value: "旧", origin: "ghost" };
    const base = baseField({ name: "total", required: true, region: origin });
    const drafts = [
      row({ rowId: "r1", name: "total", base }),
      newRow({ rowId: "n1", name: "memo", label: "備考" }),
    ];
    const regions = [
      loadedRegion(origin, { id: "g1", kind: "include", rowId: "r1" }),
      drawn({ id: "g2", rowId: "n1", exampleValue: null }),
    ];
    const body = build({ drafts, regions, mode: "edit" });
    expect(body.fields.map((f) => f.name)).toEqual(["total", "memo"]);
    expect(body.fields[0].region).toBe(origin);
    expect(body.fields[1]).toEqual({
      name: "memo",
      label: "備考",
      type: "string",
      required: false,
      critical: false,
      region: {
        page: 2,
        rect: [0.25, 0.5, 0.75, 1],
        example_value: null,
        origin: "manual",
        created_at: "2026-09-12T00:00:00.000Z",
      },
    });
  });

  it("領域の無い新規行は region: null で載る（D2。保存は止めない）", () => {
    const body = build({ drafts: [newRow({ rowId: "n1", name: "memo", label: "備考" })] });
    expect(body.fields).toEqual([
      { name: "memo", label: "備考", type: "string", required: false, critical: false, region: null },
    ]);
  });

  it("include=false の新規行は落ちる（× と同じ意味）。紐づく領域も出力に残らない", () => {
    const drafts = [
      newRow({ rowId: "n1", name: "gone", label: "消す", include: false }),
      newRow({ rowId: "n2", name: "kept", label: "残す" }),
    ];
    const regions = [drawn({ id: "g1", rowId: "n1" })];
    const body = build({ drafts, regions });
    expect(body.fields.map((f) => f.name)).toEqual(["kept"]);
    expect(body.excludeRegions).toEqual([]);
  });

  it("ゴースト由来は origin: ghost と source_quote 由来の例示値", () => {
    const regions = [drawn({ id: "g1", rowId: "n1", originKind: "ghost", exampleValue: "¥12,000" })];
    const body = build({ drafts: [newRow({ rowId: "n1", name: "total", label: "合計" })], regions });
    expect(body.fields[0].region).toMatchObject({ origin: "ghost", example_value: "¥12,000" });
  });

  it("例示値が取得中（undefined）なら example_value のキー自体を載せない。null は null で載せる", () => {
    const pending = drawn({ id: "g1", rowId: "n1", exampleValue: undefined });
    const body = build({ drafts: [newRow({ rowId: "n1", name: "a", label: "A" })], regions: [pending] });
    expect(body.fields[0].region).toEqual({
      page: 2,
      rect: [0.25, 0.5, 0.75, 1],
      origin: "manual",
      created_at: "2026-09-12T00:00:00.000Z",
    });
    expect("example_value" in body.fields[0].region!).toBe(false);

    const none = drawn({ id: "g1", rowId: "n1", exampleValue: null });
    const body2 = build({ drafts: [newRow({ rowId: "n1", name: "a", label: "A" })], regions: [none] });
    expect(body2.fields[0].region).toHaveProperty("example_value", null);
  });
});

describe("buildSaveBody: 出どころの保全と引き直し（D8・D11）", () => {
  const origin: RegionRect = {
    page: 2,
    rect: [0.12345, 0.2, 0.34567, 0.4],
    example_value: "前回の値",
    origin: "ghost",
    created_at: "2026-01-01T00:00:00+00:00",
  };
  const base = baseField({ name: "total", region: origin });

  it("触っていない既存領域は origin の RegionRect を同一参照で返す（3 項目ごと）", () => {
    const regions = [loadedRegion(origin, { id: "g1", kind: "include", rowId: "r1" })];
    const body = build({ drafts: [row({ rowId: "r1", name: "total", base })], regions });
    expect(body.fields[0].region).toBe(origin);
    expect(body.fields[0].region).toMatchObject({
      example_value: "前回の値",
      origin: "ghost",
      created_at: "2026-01-01T00:00:00+00:00",
    });
  });

  it("引き直した領域（originBbox と異なる）は origin の値を引きずらない（出どころ無しなら 3 キーとも載らない）", () => {
    const r = loadedRegion(origin, { id: "g1", kind: "include", rowId: "r1" });
    const moved: PreviewRegion = { ...r, bbox: [100, 200, 300, 400] as Px };
    const body = build({ drafts: [row({ rowId: "r1", name: "total", base })], regions: [moved] });
    expect(body.fields[0].region).toEqual({ page: 2, rect: [0.1, 0.2, 0.3, 0.4] });
    expect(body.fields[0].region).not.toHaveProperty("example_value");
    expect(body.fields[0].region).not.toHaveProperty("origin");
    expect(body.fields[0].region).not.toHaveProperty("created_at");
  });

  it("引き直した領域は新しい exampleValue（null を含む）を載せる", () => {
    const redrawn = drawn({
      id: "g2",
      rowId: "r1",
      bbox: [100, 200, 300, 400],
      exampleValue: "取り直した値",
      createdAt: "2026-09-12T01:02:03.000Z",
    });
    const body = build({ drafts: [row({ rowId: "r1", name: "total", base })], regions: [redrawn] });
    expect(body.fields[0].region).toEqual({
      page: 2,
      rect: [0.1, 0.2, 0.3, 0.4],
      example_value: "取り直した値",
      origin: "manual",
      created_at: "2026-09-12T01:02:03.000Z",
    });

    const empty = drawn({ id: "g3", rowId: "r1", bbox: [100, 200, 300, 400], exampleValue: null });
    const body2 = build({ drafts: [row({ rowId: "r1", name: "total", base })], regions: [empty] });
    expect(body2.fields[0].region).toHaveProperty("example_value", null);
  });

  it("出どころを持たない既存経路の領域は body が変わらない（3 キーとも載らない）", () => {
    const regions: PreviewRegion[] = [
      { id: "g1", kind: "include", bbox: [250, 500, 750, 1000], drawnPage: 2, page: 2, rowId: "r1" },
    ];
    const body = build({ drafts: [row({ rowId: "r1", name: "amount" })], regions });
    expect(JSON.stringify(body.fields[0].region)).toBe(
      JSON.stringify({ page: 2, rect: [0.25, 0.5, 0.75, 1] }),
    );
  });

  it("除外領域には出どころを付けない（従来どおり）", () => {
    const body = build({
      regions: [
        {
          id: "e1",
          kind: "exclude",
          bbox: [100, 200, 300, 400],
          drawnPage: 1,
          page: 1,
          originKind: "manual",
          createdAt: "2026-09-12T00:00:00.000Z",
          exampleValue: "x",
        },
      ],
    });
    expect(body.excludeRegions).toEqual([{ page: 1, rect: [0.1, 0.2, 0.3, 0.4], label: null }]);
  });
});

// ---- validateDrafts: 新規行（D5） ----

describe("validateDrafts: 新規行", () => {
  const existing = row({ rowId: "r1", name: "total" });

  it("name が空 → 文言と行番号（一覧での位置・1 始まり）", () => {
    const drafts = [existing, newRow({ rowId: "n1", name: "", label: "支払期日" })];
    expect(validateDrafts({ docType: "x", drafts, includes: [] })).toBe(
      "項目名と表示名を入力してください（2 行目）。",
    );
  });

  it("label が空（空白のみ）→ 同じ文言", () => {
    const drafts = [newRow({ rowId: "n1", name: "due_date", label: "  " }), existing];
    expect(validateDrafts({ docType: "x", drafts, includes: [] })).toBe(
      "項目名と表示名を入力してください（1 行目）。",
    );
  });

  it("空の新規行が複数あれば最初の行を指す", () => {
    const drafts = [existing, newRow({ rowId: "n1" }), newRow({ rowId: "n2" })];
    expect(validateDrafts({ docType: "x", drafts, includes: [] })).toBe(
      "項目名と表示名を入力してください（2 行目）。",
    );
  });

  it("行番号は include=false の既存行も数える（見た目の行と一致させる）", () => {
    const drafts = [row({ rowId: "r0", include: false }), existing, newRow({ rowId: "n1" })];
    expect(validateDrafts({ docType: "x", drafts, includes: [] })).toBe(
      "項目名と表示名を入力してください（3 行目）。",
    );
  });

  it("新規行にも命名規則が課される（base が無い＝新しく付けた名前）。行番号つき", () => {
    // 受け入れ条件 §1.6-3: 新規行の違反は「どの行か」が分かる（既存行の文言は変えない）
    const drafts = [existing, newRow({ rowId: "n1", name: "支払期日", label: "支払期日" })];
    expect(validateDrafts({ docType: "x", drafts, includes: [] })).toBe(
      "項目名（name）は英字始まりの英数字・アンダースコアにしてください。（2 行目）",
    );
  });

  it("新規行の name が既存行と重複。行番号つき", () => {
    const drafts = [existing, newRow({ rowId: "n1", name: "total", label: "合計" })];
    expect(validateDrafts({ docType: "x", drafts, includes: [] })).toBe(
      "項目名（name）が重複しています。（2 行目）",
    );
  });

  it("既存行どうしの重複・規則違反は従来の文言のまま（行番号なし）", () => {
    const a = row({ rowId: "a", name: "total", base: { name: "total", type: "string", required: false, critical: false } });
    const b = row({ rowId: "b", name: "total", base: { name: "total", type: "string", required: false, critical: false } });
    expect(validateDrafts({ docType: "x", drafts: [a, b], includes: [] })).toBe(
      "項目名（name）が重複しています。",
    );
  });

  it("新規行で領域が無くても止めない（D2）", () => {
    const drafts = [newRow({ rowId: "n1", name: "memo", label: "備考" })];
    expect(validateDrafts({ docType: "x", drafts, includes: [] })).toBeNull();
  });

  it("領域付きの新規行は孤立扱いにならない", () => {
    const drafts = [newRow({ rowId: "n1", name: "memo", label: "備考" })];
    expect(validateDrafts({ docType: "x", drafts, includes: [drawn({ id: "g1", rowId: "n1" })] })).toBeNull();
  });

  it("空の新規行の検査は「項目数」の後・「命名規則」の前", () => {
    // 空 name は命名規則にも引っかかるが、新規行なら先に「入力してください」で止める
    const drafts = [newRow({ rowId: "n1", name: "", label: "" })];
    expect(validateDrafts({ docType: "x", drafts, includes: [] })).toBe(
      "項目名と表示名を入力してください（1 行目）。",
    );
    expect(validateDrafts({ docType: "", drafts, includes: [] })).toBe(
      "帳票種別（doc_type）を入力してください。",
    );
    // 既存行（isNew 無し）の空 name は従来どおり命名規則の文言
    expect(
      validateDrafts({ docType: "x", drafts: [row({ rowId: "r1", name: "" })], includes: [] }),
    ).toBe("項目名（name）は英字始まりの英数字・アンダースコアにしてください。");
  });
});

// ---- resolveGhosts: 原文（D6） ----

describe("resolveGhosts: source_quote", () => {
  const bbox: Px = [10, 20, 30, 40];
  const drafts = [row({ rowId: "r1", name: "total" })];
  const ghosts = (f: ExtractedField) =>
    resolveGhosts({ fields: [f], drafts, regionByRow: new Map(), page: 1, mode: "create" });

  it("source_quote を quote に載せる", () => {
    const f = field({ name: "total", label: "合計", page: 1, bbox, source_quote: "合計 ¥12,000" });
    expect(ghosts(f)).toEqual([{ key: "total", bbox, label: "合計", quote: "合計 ¥12,000" }]);
  });

  it("40 字で切る（コードポイント単位）", () => {
    const long = "あ".repeat(39) + "𠮷" + "い".repeat(10); // 41 文字目以降を落とす
    const f = field({ name: "total", page: 1, bbox, source_quote: long });
    const [g] = ghosts(f);
    expect(Array.from(g.quote!)).toHaveLength(GHOST_QUOTE_MAX_LEN);
    expect(g.quote).toBe("あ".repeat(39) + "𠮷");
  });

  it("source_quote が無い／空白だけなら quote 無し（bbox があればゴーストは出る）", () => {
    for (const q of [undefined, null, "", "   "]) {
      const [g] = ghosts(field({ name: "total", page: 1, bbox, source_quote: q }));
      expect(g).toEqual({ key: "total", bbox, label: "total" });
      expect(g).not.toHaveProperty("quote");
    }
  });

  it("前後の空白は落とす", () => {
    const [g] = ghosts(field({ name: "total", page: 1, bbox, source_quote: "  ¥1  " }));
    expect(g.quote).toBe("¥1");
  });
});

// ---- spansInRect / exampleValueFromSpans（D11） ----

function span(span_id: number, text: string, bbox: RunSpanDto["bbox"]): RunSpanDto {
  return { span_id, text, bbox };
}

describe("spansInRect", () => {
  const rect: Px = [100, 100, 200, 200];

  it("span の中心点が矩形内なら採る（重なりが 30% 未満でも）", () => {
    // 300×300 の span の中心 (150,150) は矩形内。重なりは 100×100 / 90000 ≒ 11% しか無い
    expect(spansInRect([span(1, "a", [0, 0, 300, 300])], rect)).toHaveLength(1);
    // 中心 (240,150) が外で、重なりも 10×20 / 100×20 = 10% → 落ちる
    expect(spansInRect([span(2, "b", [190, 140, 290, 160])], rect)).toHaveLength(0);
  });

  it("中心点が外でも、重なりが span 面積の 30% 以上なら採る（29% は落ちる）", () => {
    // span 幅 100 高さ 20（面積 2000）。矩形との重なり幅 30 → 600（30%）→ 採る
    expect(spansInRect([span(1, "a", [170, 140, 270, 160])], rect)).toHaveLength(1);
    // 重なり幅 29 → 580（29%）→ 落ちる
    expect(spansInRect([span(2, "b", [171, 140, 271, 160])], rect)).toHaveLength(0);
  });

  it("境界: 中心点が矩形の辺の上なら採る", () => {
    expect(spansInRect([span(1, "a", [190, 90, 210, 110])], rect)).toHaveLength(1); // 中心 (200,100)
    expect(spansInRect([span(2, "b", [90, 190, 110, 210])], rect)).toHaveLength(1); // 中心 (100,200)
  });

  it("面積の無い span は中心点だけで判定する", () => {
    expect(spansInRect([span(1, "a", [150, 150, 150, 150])], rect)).toHaveLength(1);
    expect(spansInRect([span(2, "b", [50, 50, 50, 50])], rect)).toHaveLength(0);
  });

  it("bbox の無い span は対象外", () => {
    expect(spansInRect([span(1, "a", null), span(2, "b", undefined)], rect)).toEqual([]);
  });

  it("読み順（span_id 昇順）で返す。入力順には依らない", () => {
    const inside: RunSpanDto["bbox"] = [110, 110, 190, 190];
    const out = spansInRect([span(30, "c", inside), span(10, "a", inside), span(20, "b", inside)], rect);
    expect(out.map((s) => s.span_id)).toEqual([10, 20, 30]);
  });

  it("逆向きの矩形（右下→左上のドラッグ）も同じ結果", () => {
    const s = span(1, "a", [110, 110, 190, 190]);
    expect(spansInRect([s], [200, 200, 100, 100])).toEqual([s]);
  });

  it("入力を変更しない", () => {
    const spans = [span(2, "b", [110, 110, 190, 190]), span(1, "a", [110, 110, 190, 190])];
    const snapshot = JSON.stringify(spans);
    spansInRect(spans, rect);
    expect(JSON.stringify(spans)).toBe(snapshot);
  });
});

describe("exampleValueFromSpans", () => {
  const rect: Px = [100, 100, 200, 200];
  const inside: RunSpanDto["bbox"] = [110, 110, 190, 190];
  const run = (spans: RunSpanDto[], page_no = 2): RunSpans => ({ run_id: "run-1", page_no, spans });

  it("枠の中の span を読み順に空白で連結する", () => {
    const r = run([span(3, "1日", inside), span(1, "令和", inside), span(2, "5年", inside)]);
    expect(exampleValueFromSpans(r, 2, rect)).toBe("令和 5年 1日");
  });

  it("別ページの応答は使わない（null）", () => {
    const r = run([span(1, "a", inside)], 1);
    expect(exampleValueFromSpans(r, 2, rect)).toBeNull();
    expect(exampleValueFromSpans(r, 1, rect)).toBe("a");
  });

  it("該当 0 件・API 失敗（null / undefined）は null", () => {
    expect(exampleValueFromSpans(run([span(1, "a", [0, 0, 10, 10])]), 2, rect)).toBeNull();
    expect(exampleValueFromSpans(run([]), 2, rect)).toBeNull();
    expect(exampleValueFromSpans(null, 2, rect)).toBeNull();
    expect(exampleValueFromSpans(undefined, 2, rect)).toBeNull();
  });

  it("空白だけの span は連結から除き、残らなければ null", () => {
    expect(exampleValueFromSpans(run([span(1, "  ", inside), span(2, "x ", inside)]), 2, rect)).toBe("x");
    expect(exampleValueFromSpans(run([span(1, "  ", inside)]), 2, rect)).toBeNull();
  });

  it("200 字で切る（サーバの上限と同じ・コードポイント単位）", () => {
    const r = run([span(1, "あ".repeat(150), inside), span(2, "𠮷".repeat(100), inside)]);
    const v = exampleValueFromSpans(r, 2, rect)!;
    expect(Array.from(v)).toHaveLength(EXAMPLE_VALUE_MAX_LEN);
    expect(v).toBe("あ".repeat(150) + " " + "𠮷".repeat(49));
  });

  it("正規化はしない（全角・記号・大文字小文字をそのまま）", () => {
    const r = run([span(1, "株式会社　ＡＢＣ", inside), span(2, "¥1,000-", inside)]);
    expect(exampleValueFromSpans(r, 2, rect)).toBe("株式会社　ＡＢＣ ¥1,000-");
  });
});

describe("exampleValueFromQuote / truncateChars", () => {
  it("source_quote を trim して載せ、無ければ null", () => {
    expect(exampleValueFromQuote(" ¥12,000 ")).toBe("¥12,000");
    for (const q of [undefined, null, "", "  "]) expect(exampleValueFromQuote(q)).toBeNull();
  });

  it("200 字で切る", () => {
    expect(Array.from(exampleValueFromQuote("x".repeat(300))!)).toHaveLength(EXAMPLE_VALUE_MAX_LEN);
  });

  it("truncateChars はサロゲートペアを割らない。上限以下ならそのまま返す", () => {
    expect(truncateChars("a𠮷b", 2)).toBe("a𠮷");
    expect(truncateChars("abc", 3)).toBe("abc");
    expect(truncateChars("", 3)).toBe("");
  });
});

describe("buildSaveBody: 例示値を消す（§2.3）", () => {
  it("触っていない矩形でも exampleCleared なら原本のまま example_value だけ null にする", () => {
    const origin = {
      page: 1,
      rect: [0.1, 0.1, 0.3, 0.2] as [number, number, number, number],
      example_value: "大熊 和一",
      origin: "ghost" as const,
      created_at: "2026-09-11T00:00:00Z",
    };
    const px = denormalize(origin.rect, 1000, 2000);
    const drafts = [
      row({
        rowId: "r1",
        name: "customer",
        base: { name: "customer", type: "string", required: false, critical: false, region: origin },
      }),
    ];
    const region = {
      id: "g1",
      kind: "include" as const,
      bbox: px,
      drawnPage: 1,
      page: 1,
      rowId: "r1",
      origin,
      originBbox: px,
      exampleValue: null,
      exampleCleared: true,
    };
    const body = buildSaveBody({
      drafts,
      regions: [region],
      preserved: { fieldRegions: {}, excludes: [], sourcePageCount: null },
      dimsUnavailable: false,
      pageCount: 1,
      pageDims: [{ page_no: 1, width: 1000, height: 2000 }],
      mode: "edit",
    });
    const saved = body.fields[0].region!;
    expect(saved.rect).toEqual(origin.rect); // 矩形は再正規化しない（丸め往復なし）
    expect(saved.example_value).toBeNull();
    expect(saved.origin).toBe("ghost"); // 出どころ・作成時刻は保つ
    expect(saved.created_at).toBe("2026-09-11T00:00:00Z");
  });
});
