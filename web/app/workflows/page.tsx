"use client";

// SCR-07 ワークフロー一覧（§16 P7）。作成 → エディタへ。
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { AppShell } from "@/components/AppShell";
import { StatusChip } from "@/components/StatusChip";
import { ApiError, api } from "@/lib/api";
import { useToasts } from "@/lib/toast";
import type { WorkflowListItemDto } from "@/lib/types";
import { STALE_SCHEMA_BADGE, staleSchemaTitle } from "@/lib/workflowStale";

// 新規作成の雛形: 手動トリガー → 抽出。エディタで育てる前提の最小 DAG
const TEMPLATE_GRAPH = {
  version: 1,
  nodes: [
    { id: "t1", type: "source.manual", config: {}, pos: [80, 160] as [number, number] },
    {
      id: "x1",
      type: "process.extract",
      config: { schema_id: "sch_inv" },
      pos: [340, 160] as [number, number],
    },
  ],
  edges: [{ from: "t1", to: "x1" }],
};

function AdminDenied() {
  return (
    <AppShell active="workflows">
      <div className="topbar">
        <span className="ttl">ワークフロー</span>
      </div>
      <div className="access-denied">
        <div style={{ fontSize: 40 }}>🔒</div>
        <h2>権限がありません</h2>
        <p>この画面は管理者（admin）のみ利用できます。</p>
      </div>
    </AppShell>
  );
}

export default function WorkflowsPage() {
  const qc = useQueryClient();
  const router = useRouter();
  const push = useToasts((s) => s.push);
  const { data, error } = useQuery({
    queryKey: ["workflows"],
    queryFn: () => api.listWorkflows(),
  });

  const create = useMutation({
    mutationFn: () => api.createWorkflow("新しいワークフロー", TEMPLATE_GRAPH),
    onSuccess: (w) => {
      qc.invalidateQueries({ queryKey: ["workflows"] });
      router.push(`/workflows/${w.id}`);
    },
    onError: (e) =>
      push({ kind: "warn", message: `作成に失敗しました（${(e as Error).message}）。` }),
  });

  // 削除（C9-D）。draft/paused で実行履歴が無いものだけ消せる。active は先に停止、
  // 履歴があるものは停止のまま残す（履歴の参照先が無くなる）。
  const del = useMutation({
    mutationFn: (w: WorkflowListItemDto) => api.deleteWorkflow(w.id),
    onSuccess: (_r, w) => {
      push({ kind: "ok", message: `「${w.name}」を削除しました。` });
      qc.invalidateQueries({ queryKey: ["workflows"] });
    },
    onError: (e, w) => {
      // instanceof は dev のモジュール重複で false になり得るため status/details を直接見る
      const err = e as { status?: number; details?: Record<string, unknown> };
      if (err.status === 409) {
        const reason = String(err.details?.reason ?? "");
        push({
          kind: "warn",
          message:
            reason === "active"
              ? `「${w.name}」は有効なため削除できません。先に停止してください。`
              : reason === "has_runs"
                ? `「${w.name}」には実行履歴があるため削除できません。停止のまま残してください。`
                : `「${w.name}」は有効化または実行されたため削除できません。一覧を更新してください。`,
        });
        qc.invalidateQueries({ queryKey: ["workflows"] });
        return;
      }
      push({ kind: "err", message: `削除できませんでした（${(e as Error).message}）。` });
    },
  });

  function confirmThenDelete(w: WorkflowListItemDto) {
    const ok = window.confirm(
      `ワークフロー「${w.name}」（v${w.version}）を削除します。\n` +
        "定義は消え、元に戻せません（実行履歴があるものは削除できず、停止のまま残ります）。\n\n" +
        "よろしいですか？",
    );
    if (ok) del.mutate(w);
  }

  if (error instanceof ApiError && error.status === 403) return <AdminDenied />;

  return (
    <AppShell active="workflows">
      <div className="topbar">
        <span className="ttl">ワークフロー</span>
        <span className="sub">トリガー → 抽出 → 分岐 → 出力の自動化（§16）</span>
        <button
          className="btn primary"
          style={{ marginLeft: "auto" }}
          onClick={() => create.mutate()}
          disabled={create.isPending}
        >
          ＋ 新規作成
        </button>
      </div>
      <div className="wf-list">
        {(data?.items ?? []).map((w) => {
          // 版固定の extract ノードが旧版を指している（設計 §4.4b / §11-9）。判定はサーバ。
          // 以前は保存後トーストと lint L012 だけで、一覧からは分からなかった
          const staleTitle = staleSchemaTitle(w.stale_schema_refs);
          return (
            <Link key={w.id} href={`/workflows/${w.id}`} className="wf-card">
              <div className="wf-card-head">
                <span className="wf-name">{w.name}</span>
                <StatusChip status={w.status} />
                {staleTitle && (
                  <span className="wf-stale" title={staleTitle}>
                    {STALE_SCHEMA_BADGE}
                  </span>
                )}
                {w.status !== "active" && (
                  // カード全体がリンクなので、ボタンはリンク遷移を止めてから動かす。
                  // active には出さない（押させてから 409 で断るのは最悪の順序。先に停止）
                  <button
                    className="btn sm danger"
                    style={{ marginLeft: "auto" }}
                    disabled={del.isPending && del.variables?.id === w.id}
                    title="このワークフローを削除する（元に戻せません。実行履歴があるものは削除できません）"
                    onClick={(e) => {
                      e.preventDefault();
                      e.stopPropagation();
                      confirmThenDelete(w);
                    }}
                  >
                    {del.isPending && del.variables?.id === w.id ? "削除中…" : "削除"}
                  </button>
                )}
              </div>
              <div className="wf-card-sub">
                v{w.version}
                {w.updated_at ? ` ・ 更新 ${new Date(w.updated_at).toLocaleString("ja-JP")}` : ""}
                <span className="sub" style={{ marginLeft: 8 }}>
                  {w.id}
                </span>
              </div>
            </Link>
          );
        })}
        {data && data.items.length === 0 && (
          <div className="empty">
            まだワークフローがありません。「＋ 新規作成」から始めてください。
          </div>
        )}
      </div>
    </AppShell>
  );
}
