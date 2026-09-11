// TemplatizePreview から切り出した純粋関数の単体テスト（設計 v2 §1.4・§6）。
//
// ここで固定するのは「切り出し前の save() / issue() / ghosts と同じ出力」であって、
// 望ましい振る舞いの提案ではない。文言・分岐・順序は現状のまま写している。

import { describe, expect, it } from "vitest";

import type { ExtractedField, PageDim, RegionRect, SchemaFieldDto } from "@/lib/types";

import {
  buildSaveBody,
  denormalize,
  normalize,
  resolveGhosts,
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
