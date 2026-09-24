// 記入例語の例示値の警告（設計 region-field-add-and-hint-v2 §2.5 / region-template-editor §6）。
//
// 例示値が記入例語（「〇〇株式会社」「YYYY/MM/DD」「住所1」）だと、実行時は KIE の前の
// 事前ガードがその項目の位置のヒントを落とす（placeholder_example）。保存は止めないが、
// 黙っていると作者は「記入例の帳票で領域を引いた」ことに気付けないので、テンプレート化／
// 領域編集画面の行と、保存後の通知で伝える。
//
// **判定はここで書かない。** 規則はサーバ（newfan_schemas.is_placeholder_example。orchestrator
// の事前ガードと同じ関数）にあり、画面は POST /schemas/example-values/check と PUT /schemas の
// 応答（warnings）で結果だけを受け取る。TypeScript に書き直すと、画面の警告と実行時の判定が
// ずれる（規則は 4 つの正規表現と語彙で、第 5 回計測の敵対的検証で直し続けている）。
// ここに置くのは、問い合わせる値の選び方・結果の持ち方・通知の文言という純粋な部分だけ。

import type { ExampleValueCheckResponse, SchemaSaveWarning } from "@/lib/types";

/** 判定済みの例示値 → 記入例語か。画面の state に持ち、同じ値は 2 度問い合わせない。 */
export type PlaceholderVerdicts = ReadonlyMap<string, boolean>;

/**
 * まだ判定していない例示値を、重複を除いて出現順に返す。null / undefined / 空白だけの値は
 * 問い合わせない（例示値なしと同じで、記入例語にはならない）。
 */
export function unknownExampleValues(
  values: readonly (string | null | undefined)[],
  verdicts: PlaceholderVerdicts,
): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  for (const v of values) {
    if (v == null || !v.trim() || verdicts.has(v) || seen.has(v)) continue;
    seen.add(v);
    out.push(v);
  }
  return out;
}

/**
 * 問い合わせの結果を判定済みに足した新しい Map を返す（state は置き換える）。
 * 応答は送った順・同じ件数で返る約束だが、件数が合わないときは何も足さない
 * （ずれたまま足すと別の値に注記が付く。足さなければ注記が出ないだけ）。
 */
export function mergeVerdicts(
  prev: PlaceholderVerdicts,
  sent: readonly string[],
  res: ExampleValueCheckResponse,
): Map<string, boolean> {
  const next = new Map(prev);
  if (res.items.length !== sent.length) return next;
  sent.forEach((v, i) => next.set(v, res.items[i].placeholder === true));
  return next;
}

/** この例示値に「記入例の語のようです」の注記を出すか（判定済みで、記入例語のときだけ）。 */
export function isPlaceholderExample(
  value: string | null | undefined,
  verdicts: PlaceholderVerdicts,
): boolean {
  return value != null && verdicts.get(value) === true;
}

/** 行の注記。例示値を消すと注記も消える（消した例示値は保存されない） */
export const PLACEHOLDER_ROW_NOTE =
  "⚠ 記入例の語のようです。この例示値は位置のヒントに使われません（例示値を消す で外せます）";

/** 通知に並べる例示値の上限（残りは件数だけ） */
const WARNING_LIST_MAX = 3;
/** 通知に載せる例示値 1 件あたりの上限（コードポイント単位） */
const WARNING_VALUE_MAX_LEN = 24;

function clip(s: string, n: number): string {
  const cps = Array.from(s);
  return cps.length > n ? `${cps.slice(0, n).join("")}…` : s;
}

/**
 * 保存後の通知（PUT /schemas の応答の warnings から）。記入例語の例示値が無ければ null。
 * `labelOf` は項目名 → 表示名（無ければ項目名のまま出す）。
 */
export function placeholderSavedMessage(
  warnings: readonly SchemaSaveWarning[] | null | undefined,
  labelOf: (field: string) => string | null | undefined = () => null,
): string | null {
  const hits = (warnings ?? []).filter((w) => w.code === "placeholder_example");
  if (hits.length === 0) return null;
  const shown = hits
    .slice(0, WARNING_LIST_MAX)
    .map((w) => `${labelOf(w.field) || w.field}「${clip(w.example_value, WARNING_VALUE_MAX_LEN)}」`)
    .join("、");
  const rest = hits.length > WARNING_LIST_MAX ? ` ほか ${hits.length - WARNING_LIST_MAX} 件` : "";
  return (
    `読取領域 ${hits.length} 件の例示値が記入例の語のようです（${shown}${rest}）。` +
    "これらの項目では位置のヒントが使われません。記入例ではない帳票で領域を引き直すか、" +
    "編集画面の「例示値を消す」で外してください。"
  );
}
