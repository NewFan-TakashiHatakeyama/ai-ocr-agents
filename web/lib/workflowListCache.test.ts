// ワークフロー一覧のクエリキャッシュ（lib/workflowListCache.ts）の単体テスト。
// 一覧の「⚠ 旧版スキーマ」バッジはこのキャッシュから出るので、旧版化・解消の操作
// （スキーマの新版保存・エディタの保存・有効化・停止）の後に確実に捨てられることを固定する。

import { QueryClient } from "@tanstack/react-query";
import { afterEach, describe, expect, it } from "vitest";

import { WORKFLOWS_QUERY_KEY, invalidateWorkflowList } from "./workflowListCache";

// アプリと同じく staleTime 内（providers.tsx は 10 秒）。捨てないと再取得されない条件で見る
const clients: QueryClient[] = [];
function client(): QueryClient {
  const qc = new QueryClient({ defaultOptions: { queries: { staleTime: 10_000 } } });
  clients.push(qc);
  return qc;
}

afterEach(() => {
  for (const qc of clients.splice(0)) qc.clear();
});

describe("invalidateWorkflowList", () => {
  it("staleTime 内の一覧キャッシュを無効化する（次に表示したとき取り直す）", async () => {
    const qc = client();
    qc.setQueryData(WORKFLOWS_QUERY_KEY, { items: [] });
    expect(qc.getQueryState(WORKFLOWS_QUERY_KEY)?.isInvalidated).toBe(false);

    await invalidateWorkflowList(qc);
    expect(qc.getQueryState(WORKFLOWS_QUERY_KEY)?.isInvalidated).toBe(true);
  });

  it("エディタが捨てる ['workflow', id] は一覧に届かない（だから別に呼ぶ）", async () => {
    const qc = client();
    qc.setQueryData(WORKFLOWS_QUERY_KEY, { items: [] });
    qc.setQueryData(["workflow", "wf_1"], { id: "wf_1" });

    await qc.invalidateQueries({ queryKey: ["workflow", "wf_1"] });
    expect(qc.getQueryState(WORKFLOWS_QUERY_KEY)?.isInvalidated).toBe(false);

    await invalidateWorkflowList(qc);
    expect(qc.getQueryState(WORKFLOWS_QUERY_KEY)?.isInvalidated).toBe(true);
  });
});
