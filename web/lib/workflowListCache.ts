// ワークフロー一覧（GET /workflows）のクエリキャッシュ。キーと捨て方をここに 1 つだけ置く。
//
// 一覧（SCR-07）の「⚠ 旧版スキーマ」バッジは、このキャッシュの stale_schema_refs から出る
// （設計 region-template-editor §4.4b-3）。旧版参照は一覧の外の操作で変わる:
//  - スキーマの新版保存（テンプレート化・領域編集 = useSchemaSaved、スキーマ管理画面）で
//    それまで最新だった版を固定しているワークフローが旧版参照になる
//  - エディタの保存（extract ノードのスキーマを選び直す）で解消する。有効化・停止は
//    一覧の status（バッジの隣）が変わる
// これらの後に捨てないと、staleTime（providers.tsx の 10 秒）の間に一覧へ戻ったときや、
// 同じキャッシュを持つ RunWorkflow が生きている間、バッジと status が古いまま出る。
// エディタが捨てる ["workflow", id] は ["workflows"] と prefix が違うので一覧には届かない。

import type { QueryClient } from "@tanstack/react-query";

/** 一覧のクエリキー。一覧画面と RunWorkflow（帳票詳細の「ワークフローで処理」）が共有する */
export const WORKFLOWS_QUERY_KEY = ["workflows"] as const;

/** 一覧のキャッシュを捨てる（表示中なら再取得、そうでなければ次に表示したときに取り直す） */
export function invalidateWorkflowList(qc: QueryClient): Promise<void> {
  return qc.invalidateQueries({ queryKey: WORKFLOWS_QUERY_KEY });
}
