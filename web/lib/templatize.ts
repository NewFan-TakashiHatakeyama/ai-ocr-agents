// テンプレート化プレビュー（TemplatizePreview）の純粋な計算部分（設計 v2 §1.4）。
//
// 保存 body の生成・保存前の検査・ゴーストの解決・枠の下の文字（例示値）の計算を、
// DOM と React に依存しない関数としてここに置く。TemplatizePreview.tsx は state と
// イベントを持ち、計算はここの関数を呼ぶだけにする。壊れやすさの本体はここなので、
// 単体テスト（templatize.test.ts）はここにだけ付ける。
//
// 型だけの import は実行時に消えるので、RegionCanvas（"use client"）を
// 読み込むことはない（vitest は node 環境で動く）。

import type { CanvasGhost, Px } from "@/components/RegionCanvas";
import type {
  ExtractedField,
  PageDim,
  RegionRect,
  RunSpanDto,
  RunSpans,
  SchemaFieldDto,
} from "@/lib/types";

// 画素矩形の型は RegionCanvas が定義元。ここを使う側（テスト等）が
// キャンバスを import しなくて済むよう型だけ再公開する。
export type { Px };

export type DraftRow = {
  rowId: string; // 領域との紐付けは name でなくこの id（rename しても外れない）
  name: string;
  label: string;
  type: string;
  include: boolean;
  sample: string;
  base?: SchemaFieldDto; // 編集モードでプリロードした元フィールド（丸ごと保全する）
  // この画面で足した行（設計 v2 §1.3）。base が無いので保存では新しい項目の分岐に
  // 落ちる。name / label の両方が必須（D5）、型に table は選べない（D7）、
  // 「残す」の代わりに × で行ごと消す（include=false と同じ意味）。
  isNew?: boolean;
};

export type PreviewRegion = {
  id: string;
  kind: "include" | "exclude";
  bbox: Px; // 画像 px（前処理後 PNG 座標）。保存時にのみ正規化する
  drawnPage: number; // この px がどのページの寸法に対するものか
  page: number | "last" | null; // 保存する適用範囲（include は必ず drawnPage）
  rowId?: string; // include のみ
  label?: string; // exclude のみ（「印影」等・任意）
  // 読み込み時の原本と、その時点の画素矩形。触っていない矩形を保存し直すときは
  // **原本をそのまま**返すために持つ。正規化 → 画素（round）→ 正規化 の往復は
  // 0.5px ぶんの丸めが乗るので、開いて保存するだけで座標が動き、開くたびに
  // ずれが積み上がる（設計の受け入れ条件「矩形を触らなければ完全一致」に反する）。
  origin?: RegionRect;
  originBbox?: Px;
  // --- 以下、この画面で作った include 領域の出どころ（設計 v2 D8・D11）。保存時に
  // RegionRect の origin / created_at / example_value に載せる。**undefined のキーは
  // 保存 body に載せない**（旧来の経路で作った領域の body を 1 バイトも変えない）。
  // 触っていない既存領域は origin（原本）をそのまま返すので、ここは見ない。
  //
  // 名前が originKind なのは、origin が既に「読み込んだ原本」の意味で使われているため。
  originKind?: "ghost" | "manual"; // ghost = AI の位置をクリックで採った / manual = 手描き
  createdAt?: string; // ISO 8601
  // ゴースト由来は source_quote、手描きは枠の下の span の原文（取得は非同期）。
  // undefined = まだ取得中、null = 取れなかった（該当 0 件・API 失敗）。
  exampleValue?: string | null;
  /** 「例示値を消す」を押した。触っていない矩形でも example_value: null で保存する（§2.3） */
  exampleCleared?: boolean;
};

/**
 * **編集できないまま保全する領域**。ページ寸法（pages.width/height）が取れないと
 * 正規化⇄画素の変換ができず矩形を描けないが、だからといって落としてはいけない。
 * 落とすと保存が全置換なので、開いて保存しただけで既存の設定が消える
 * （実機で再現した事故: ページ寸法 API が失敗した窓で領域が全消去された）。
 * 読み込んだ値をそのまま持ち回し、保存時に無変換で戻す。
 */
export type Preserved = {
  fieldRegions: Record<string, RegionRect>; // rowId -> 元の region
  excludes: RegionRect[];
  sourcePageCount: number | null;
};

/** 画素矩形が読み込み時から変わっていないか（原本をそのまま返してよいか）。 */
export function isUntouched(r: PreviewRegion): boolean {
  return (
    !!r.origin &&
    !!r.originBbox &&
    r.originBbox.every((v, i) => v === r.bbox[i])
  );
}

// ゴースト確定時の自動パディング。タイトな外接矩形をそのまま保存すると、
// スキャンの分散だけで位置ガードが誤検知する。
export function padOf(w: number, h: number) {
  return Math.max(Math.round(Math.min(w, h) * 0.02), 12);
}

// ---- 座標変換 ----

export function normalize(b: Px, w: number, h: number): [number, number, number, number] {
  const clamp = (v: number) => Math.min(1, Math.max(0, v));
  return [clamp(b[0] / w), clamp(b[1] / h), clamp(b[2] / w), clamp(b[3] / h)];
}

export function denormalize(rect: number[], w: number, h: number): Px {
  return [
    Math.round(rect[0] * w),
    Math.round(rect[1] * h),
    Math.round(rect[2] * w),
    Math.round(rect[3] * h),
  ];
}

export function resolvePage(page: number | "last" | null | undefined, pageCount: number): number {
  if (page === "last") return pageCount;
  if (typeof page === "number") return page;
  return 1; // 全ページ指定は 1 ページ目の座標系で編集する（適用範囲は一覧で示す）
}

// ---- 保存前の検査 ----

/**
 * 保存を止める理由を返す（無ければ null）。`includes` は kind === "include" の
 * 領域だけを渡す（孤立領域の検査に使う）。
 */
export function validateDrafts({
  docType,
  drafts,
  includes,
}: {
  docType: string;
  drafts: DraftRow[];
  includes: PreviewRegion[];
}): string | null {
  const dt = docType.trim();
  if (!dt) return "帳票種別（doc_type）を入力してください。";
  const chosen = drafts.filter((d) => d.include);
  if (chosen.length === 0) return "抽出する項目を 1 つ以上選んでください。";
  // 新規行は name と label の**両方**が必須（D5）。label は KIE がその項目を探す
  // 唯一の語彙の手掛かりで、空だと name（例: due_date）が代用されて日本語帳票では
  // 探せない。行番号は一覧の見た目（1 始まり・include=false の行も数える）に合わせる。
  // 領域が無いことは止めない（D2。AI が語彙から探す）。
  const blank = drafts.findIndex((d) => d.isNew && (!d.name.trim() || !d.label.trim()));
  if (blank >= 0) return `項目名と表示名を入力してください（${blank + 1} 行目）。`;
  const names = chosen.map((d) => d.name.trim());
  // 命名規則は**新しく付けた／変えた名前**にだけ課す。chat の項目追加は任意の
  // 名前を通すので、既存スキーマには日本語名の項目があり得る。既存名まで弾くと
  // 「開いて保存するだけ」ができないスキーマができてしまう。
  const renamed = chosen.filter((d) => d.name.trim() !== d.base?.name);
  const badName = renamed.find((d) => !/^[A-Za-z][A-Za-z0-9_]*$/.test(d.name.trim()));
  if (badName) return `項目名（name）は英字始まりの英数字・アンダースコアにしてください。${rowRef(drafts, badName)}`;
  if (new Set(names).size !== names.length) {
    // どの行かが分かるように、重複している名前のうち**新規行**のものを指す（設計 §1.6-3）。
    // 既存行どうしの重複は従来の文言のまま（既存の 38 テストと契約を変えない）。
    const seen = new Map<string, number>();
    names.forEach((n) => seen.set(n, (seen.get(n) ?? 0) + 1));
    const dupNew = chosen.find((d) => d.isNew && (seen.get(d.name.trim()) ?? 0) > 1);
    return `項目名（name）が重複しています。${dupNew ? rowRef(drafts, dupNew) : ""}`;
  }
  const orphan = includes.find((r) => !drafts.some((d) => d.rowId === r.rowId && d.include));
  if (orphan) return "項目に紐づいていない読取領域があります。項目を選び直してください。";
  return null;
}

/** 新規行のときだけ「（N 行目）」を返す（既存行の文言を変えないため）。 */
function rowRef(drafts: DraftRow[], d: DraftRow): string {
  if (!d.isNew) return "";
  const i = drafts.findIndex((x) => x.rowId === d.rowId);
  return i >= 0 ? `（${i + 1} 行目）` : "";
}

// ---- 例示値（枠の下の文字）と切り詰め ----

/** 例示値の上限文字数。サーバ（newfan_schemas.field_schema.EXAMPLE_VALUE_MAX_LEN）と揃える。 */
export const EXAMPLE_VALUE_MAX_LEN = 200;
/** ゴーストに添える原文（source_quote）の上限（D6）。 */
export const GHOST_QUOTE_MAX_LEN = 40;
/**
 * 枠に含まれる span の判定に使う、重なりの下限（span 面積に対する比）。設計 §2.5 の
 * `_inside` と同じ規則で、除外領域の 50% 判定は流用しない（除外は「消しすぎ」が危険で
 * 厳しめが正しいが、ヒントは「拾い漏れ」が危険。手で引いた枠は文字の一部にしか
 * 掛からないことが普通で、0 件になるとヒントごと落ちる）。
 */
export const HINT_SPAN_RATIO = 0.3;

/**
 * コードポイント単位で n 文字に切る。サーバの `[:200]` は Python の文字（コード
 * ポイント）単位なので、UTF-16 の `slice` でサロゲートペアを割らないよう揃える。
 */
export function truncateChars(s: string, n: number): string {
  const cps = Array.from(s);
  return cps.length > n ? cps.slice(0, n).join("") : s;
}

/**
 * 画素矩形 bbox に含まれる span を**読み順（span_id 昇順）**で返す。採る条件は
 * 「span の中心点が矩形内（境界を含む）」または「重なりが span 面積の 30% 以上」の
 * どちらか。bbox の無い span は対象外。逆向きの矩形は min/max で吸収する。
 */
export function spansInRect(spans: readonly RunSpanDto[], bbox: Px): RunSpanDto[] {
  const left = Math.min(bbox[0], bbox[2]);
  const right = Math.max(bbox[0], bbox[2]);
  const top = Math.min(bbox[1], bbox[3]);
  const bottom = Math.max(bbox[1], bbox[3]);
  return spans
    .filter((s) => {
      const b = s.bbox;
      if (!b) return false;
      const sx1 = Math.min(b[0], b[2]);
      const sx2 = Math.max(b[0], b[2]);
      const sy1 = Math.min(b[1], b[3]);
      const sy2 = Math.max(b[1], b[3]);
      const cx = (sx1 + sx2) / 2;
      const cy = (sy1 + sy2) / 2;
      if (cx >= left && cx <= right && cy >= top && cy <= bottom) return true;
      const area = (sx2 - sx1) * (sy2 - sy1);
      if (area <= 0) return false; // 面積の無い span は中心点だけで判定する
      const ix = Math.max(0, Math.min(right, sx2) - Math.max(left, sx1));
      const iy = Math.max(0, Math.min(bottom, sy2) - Math.max(top, sy1));
      return (ix * iy) / area >= HINT_SPAN_RATIO;
    })
    .sort((a, b) => a.span_id - b.span_id);
}

/**
 * 除外領域の判定に使う、重なりの下限（span 面積に対する比）。サーバの
 * `region_mask.EXCLUDE_SPAN_RATIO` と同じ値にしておく（表示と実際の除外がずれない）。
 */
export const EXCLUDE_SPAN_RATIO = 0.5;

/**
 * 除外領域 bbox がこの帳票で**実際に消す** span を読み順（span_id 昇順）で返す。規則は
 * サーバの `filter_spans` と同じ: 「重なりが span 面積の 50% 以上」。面積の無い span
 * （退化 bbox）は中心点包含で判定する。bbox の無い span は対象外。逆向きの矩形は
 * min/max で吸収する。ヒント用の `spansInRect`（30%・中心点）は流用しない —— 除外は
 * 「消しすぎ」が危険で、保存前に見せたいのは実際に消える件数だから。
 */
export function spansExcludedByRect(spans: readonly RunSpanDto[], bbox: Px): RunSpanDto[] {
  const left = Math.min(bbox[0], bbox[2]);
  const right = Math.max(bbox[0], bbox[2]);
  const top = Math.min(bbox[1], bbox[3]);
  const bottom = Math.max(bbox[1], bbox[3]);
  return spans
    .filter((s) => {
      const b = s.bbox;
      if (!b) return false;
      const sx1 = Math.min(b[0], b[2]);
      const sx2 = Math.max(b[0], b[2]);
      const sy1 = Math.min(b[1], b[3]);
      const sy2 = Math.max(b[1], b[3]);
      const area = (sx2 - sx1) * (sy2 - sy1);
      if (area <= 0) {
        const cx = (sx1 + sx2) / 2;
        const cy = (sy1 + sy2) / 2;
        return cx >= left && cx <= right && cy >= top && cy <= bottom;
      }
      const ix = Math.max(0, Math.min(right, sx2) - Math.max(left, sx1));
      const iy = Math.max(0, Math.min(bottom, sy2) - Math.max(top, sy1));
      return (ix * iy) / area >= EXCLUDE_SPAN_RATIO;
    })
    .sort((a, b) => a.span_id - b.span_id);
}

/**
 * 手描き領域の例示値（D11）。`run` は GET /documents/{id}/spans の応答（ページごとに
 * キャッシュしたもの）で、取得に失敗していれば null を渡す。**別ページの応答は使わない**
 * （page_no が一致しなければ null）。枠に文字が無ければ null。span の原文を読み順に
 * 空白 1 つで連結し、前後空白を除いて 200 字で切る（サーバの消毒と同じ上限）。
 * 正規化はしない（紙面の見た目に近いほど照合しやすい）。
 */
export function exampleValueFromSpans(
  run: RunSpans | null | undefined,
  page: number,
  bbox: Px,
): string | null {
  if (!run || run.page_no !== page) return null;
  const text = spansInRect(run.spans, bbox)
    .map((s) => s.text.trim())
    .filter((t) => t.length > 0)
    .join(" ")
    .trim();
  if (!text) return null;
  return truncateChars(text, EXAMPLE_VALUE_MAX_LEN);
}

/**
 * ゴーストをクリックして採った領域の例示値（D11）: その項目の source_quote
 * （AI が根拠にした span の原文）。無ければ null。上限は span 経由と同じ 200 字。
 */
export function exampleValueFromQuote(quote: string | null | undefined): string | null {
  const t = quote?.trim() ?? "";
  return t ? truncateChars(t, EXAMPLE_VALUE_MAX_LEN) : null;
}

/**
 * この画面で作った領域の出どころを RegionRect に載せる。**undefined のキーは載せない**:
 * 旧来の経路（出どころを持たない PreviewRegion）の body を 1 バイトも変えないため。
 * 値が null（例示値が取れなかった）は null のまま載せる（前の値を引きずらない）。
 */
function withProvenance(rect: RegionRect, r: PreviewRegion): RegionRect {
  if (r.exampleValue !== undefined) rect.example_value = r.exampleValue;
  if (r.originKind !== undefined) rect.origin = r.originKind;
  if (r.createdAt !== undefined) rect.created_at = r.createdAt;
  return rect;
}

// ---- 保存 body ----

export type SaveBody = {
  fields: SchemaFieldDto[];
  excludeRegions: RegionRect[];
  /** undefined のときはキー自体を送らない（api.putSchema が省略＝引き継ぎにする） */
  sourcePageCount: number | null | undefined;
};

/**
 * PUT /schemas に渡す body を組み立てる。TemplatizePreview の save() から
 * そのまま移したもので、規則は変えていない:
 *   - base 持ちは include=false でも版から落とさない
 *   - 触っていない矩形は origin（読み込んだ原本）をそのまま返す
 *     （example_value / origin / created_at ごと保全される）
 *   - 画面で編集できなかった領域（preserved）は無変換で戻す
 *   - ページ寸法が取れないときは sourcePageCount を記録しない
 * 設計 v2 で足したのは 1 点だけ: この画面で作った include 領域は、出どころ
 * （originKind / createdAt / exampleValue）を RegionRect に載せる。新規行（isNew）は
 * base が無いので、既存の「新しい項目」の分岐にそのまま落ちる。
 */
export function buildSaveBody({
  drafts,
  regions,
  preserved,
  dimsUnavailable,
  pageCount,
  pageDims,
  mode,
}: {
  drafts: DraftRow[];
  regions: PreviewRegion[];
  preserved: Preserved;
  dimsUnavailable: boolean;
  pageCount: number;
  pageDims: PageDim[];
  mode: "create" | "edit";
}): SaveBody {
  const dimsByPage = new Map<number, PageDim>();
  pageDims.forEach((p) => dimsByPage.set(p.page_no, p));
  const regionByRow = new Map<string, PreviewRegion>();
  regions.forEach((r) => r.kind === "include" && r.rowId && regionByRow.set(r.rowId, r));
  const excludes = regions.filter((r) => r.kind === "exclude");

  function regionOf(rowId: string): RegionRect | null {
    const r = regionByRow.get(rowId);
    if (!r) {
      // 画面で編集できなかった領域は、読み込んだ値をそのまま返す（消さない）
      return preserved.fieldRegions[rowId] ?? null;
    }
    if (isUntouched(r)) {
      // 丸め往復で座標を動かさない。「例示値を消す」だけは原本のまま example_value を
      // null にして返す（矩形は触っていないので再正規化しない。設計 §2.3）
      return r.exampleCleared ? { ...r.origin!, example_value: null } : r.origin!;
    }
    const d = dimsByPage.get(r.drawnPage);
    if (!d?.width || !d?.height) return preserved.fieldRegions[rowId] ?? null;
    // 引き直した領域は origin（原本）の example_value 等を**引きずらない**。載るのは
    // この PreviewRegion 自身に付いた値だけ（引き直しは必ず取り直す。D11）。
    return withProvenance({ page: r.drawnPage, rect: normalize(r.bbox, d.width, d.height) }, r);
  }

  const fields: SchemaFieldDto[] = drafts
    // base 持ちは include=false でも版から落とさない（明細定義などを消さない）
    .filter((d) => d.include || d.base)
    .map((d) => {
      const name = d.name.trim();
      const label = d.label.trim() || name;
      const region = d.include ? regionOf(d.rowId) : null;
      return d.base
        ? { ...d.base, name, label, type: d.type, region }
        : { name, label, type: d.type, required: false, critical: false, region };
    });

  // 編集できなかったぶんを先に戻してから、画面で編集したぶんを足す
  const excludeRegions: RegionRect[] = [...preserved.excludes];
  for (const r of excludes) {
    const label = r.label?.trim() || null;
    // 矩形も名前も適用範囲も変えていないなら原本をそのまま返す
    if (isUntouched(r) && r.origin!.page === r.page && (r.origin!.label ?? null) === label) {
      excludeRegions.push(r.origin!);
      continue;
    }
    const d = dimsByPage.get(r.drawnPage);
    if (!d?.width || !d?.height) continue;
    excludeRegions.push({
      page: r.page,
      rect: normalize(r.bbox, d.width, d.height),
      label,
    });
  }

  // 編集では既存の値を保つ（未記録の旧スキーマだけ今回の値で埋める）。
  // ページ寸法が取れていないときは **記録しない**: pages が空だと pageCount が
  // 1 に潰れるため、多ページ帳票のテンプレートに 1 が焼き付いてしまう。
  // 未記録（NULL）なら位置ガードはページ判定を行わないので、誤った値より安全。
  const sourcePageCount = dimsUnavailable
    ? (mode === "create" ? undefined : preserved.sourcePageCount)
    : mode === "create"
      ? pageCount
      : (preserved.sourcePageCount ?? pageCount);

  return { fields, excludeRegions, sourcePageCount };
}

// ---- ゴースト ----

/**
 * 表示中のページに出すゴースト（AI が見つけた位置の参考表示）。
 * 確定済み（領域を持つ）行には出さない。
 * AI が読んだ原文（source_quote）があれば 40 字に切って `quote` に添える（D6）。
 * 正規化後の値ではなく原文 —— 「何をどこから読んだか」が分かるのは原文だけ。
 * 無い項目は従来どおり（bbox があればゴーストは出る。quote は付かない）。
 */
export function resolveGhosts({
  fields,
  drafts,
  regionByRow,
  page,
  mode,
}: {
  fields: ExtractedField[];
  drafts: DraftRow[];
  regionByRow: Map<string, PreviewRegion>;
  page: number;
  mode: "create" | "edit";
}): CanvasGhost[] {
  if (mode !== "create" && drafts.length === 0) return [];
  const byName = new Map(drafts.map((d) => [d.name, d]));
  return fields
    .filter((f) => f.bbox && (f.page ?? 1) === page)
    .filter((f) => {
      const d = byName.get(f.name);
      return d && !regionByRow.has(d.rowId); // 確定済みの行にはゴーストを出さない
    })
    .map((f) => {
      const g: CanvasGhost = { key: f.name, bbox: f.bbox as Px, label: f.label ?? f.name };
      const quote = f.source_quote?.trim();
      if (quote) g.quote = truncateChars(quote, GHOST_QUOTE_MAX_LEN);
      return g;
    });
}
