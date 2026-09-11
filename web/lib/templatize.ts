// テンプレート化プレビュー（TemplatizePreview）の純粋な計算部分（設計 v2 §1.4）。
//
// 保存 body の生成・保存前の検査・ゴーストの解決を、DOM と React に依存しない
// 関数としてここに置く。TemplatizePreview.tsx は state とイベントを持ち、
// 計算はこの 3 関数を呼ぶだけにする。壊れやすさの本体はこの 3 関数なので、
// 単体テスト（templatize.test.ts）はここにだけ付ける。
//
// 型だけの import は実行時に消えるので、RegionCanvas（"use client"）を
// 読み込むことはない（vitest は node 環境で動く）。

import type { CanvasGhost, Px } from "@/components/RegionCanvas";
import type { ExtractedField, PageDim, RegionRect, SchemaFieldDto } from "@/lib/types";

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
  const names = chosen.map((d) => d.name.trim());
  // 命名規則は**新しく付けた／変えた名前**にだけ課す。chat の項目追加は任意の
  // 名前を通すので、既存スキーマには日本語名の項目があり得る。既存名まで弾くと
  // 「開いて保存するだけ」ができないスキーマができてしまう。
  const renamed = chosen.filter((d) => d.name.trim() !== d.base?.name);
  if (renamed.some((d) => !/^[A-Za-z][A-Za-z0-9_]*$/.test(d.name.trim())))
    return "項目名（name）は英字始まりの英数字・アンダースコアにしてください。";
  if (new Set(names).size !== names.length) return "項目名（name）が重複しています。";
  const orphan = includes.find((r) => !drafts.some((d) => d.rowId === r.rowId && d.include));
  if (orphan) return "項目に紐づいていない読取領域があります。項目を選び直してください。";
  return null;
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
 *   - 画面で編集できなかった領域（preserved）は無変換で戻す
 *   - ページ寸法が取れないときは sourcePageCount を記録しない
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
    if (isUntouched(r)) return r.origin!; // 丸め往復で座標を動かさない
    const d = dimsByPage.get(r.drawnPage);
    if (!d?.width || !d?.height) return preserved.fieldRegions[rowId] ?? null;
    return { page: r.drawnPage, rect: normalize(r.bbox, d.width, d.height) };
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
    .map((f) => ({
      key: f.name,
      bbox: f.bbox as Px,
      label: f.label ?? f.name,
    }));
}
