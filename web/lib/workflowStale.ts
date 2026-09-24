// ワークフロー一覧（SCR-07）の「旧版スキーマ」バッジの純粋関数。表示の導出だけで DOM・API に触らない。
//
// 旧版かどうかの判定はサーバ（GET /workflows の stale_schema_refs）が行う。stale-workflows
// （保存後トースト）・lint L012 と同じ判定で、web 側に判定ロジックを持たない
// （設計 region-template-editor §4.4b / §11-9。web で graph_json を突合していた頃は
// 「直前の版」しか見えず v1 固定が漏れた。第 3 回敵対的レビュー 2）。

import type { StaleSchemaRefDto } from "./types";

/** バッジの文言 */
export const STALE_SCHEMA_BADGE = "⚠ 旧版スキーマ";

/** 内訳 1 行: 「x1: invoice v2 → 最新 v4」 */
export function staleSchemaLine(ref: StaleSchemaRefDto): string {
  return `${ref.node_id}: ${ref.doc_type} v${ref.schema_version} → 最新 v${ref.latest_version}`;
}

/**
 * バッジの title（ホバーで出す内訳）。旧版参照が無ければ null（バッジを出さない）。
 * 行はサーバの順（graph_json のノード順）のまま並べる。
 */
export function staleSchemaTitle(
  refs: readonly StaleSchemaRefDto[] | null | undefined,
): string | null {
  if (!refs || refs.length === 0) return null;
  return [
    `旧版のスキーマを参照している抽出ノードがあります（${refs.length} 件）`,
    ...refs.map((r) => `・${staleSchemaLine(r)}`),
    "版を固定したノードには、テンプレート化や領域編集で保存した新版が自動適用されません。" +
      "エディタで extract ノードのスキーマを選び直すか、「帳票種別（常に最新版を使う）」に" +
      "切り替えてください（有効なワークフローは保存後に再有効化が必要です）。",
  ].join("\n");
}
