"use client";

// 手元の帳票を active なワークフローに手動投入する導線（④）。
// バックエンド（POST /workflows/{id}/runs）は実装済みだが web に呼ぶ導線が無かった。
// ドキュメント詳細から「このワークフローで処理」を選んで実行できるようにする。

import { useMutation, useQuery } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { api } from "@/lib/api";
import { useToasts } from "@/lib/toast";

export function RunWorkflow({ documentId }: { documentId: string }) {
  const push = useToasts((s) => s.push);
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [wfId, setWfId] = useState<string>("");

  const workflows = useQuery({ queryKey: ["workflows"], queryFn: () => api.listWorkflows() });
  // 手動投入できるのは有効化済み（active）のワークフローだけ（§11.1 版固定）
  const active = (workflows.data?.items ?? []).filter((w) => w.status === "active");
  const effective = wfId || active[0]?.id || "";

  const run = useMutation({
    mutationFn: () => api.startWorkflowRun(effective, documentId),
    onSuccess: () => {
      setOpen(false);
      push({
        kind: "ok",
        message: "ワークフローを開始しました。人手確認が要る場合はレビューキューに入ります。",
      });
      // 実行状況を追えるようレビューキューへ誘導
      router.push("/documents?tab=queue");
    },
    onError: (e) =>
      push({ kind: "warn", message: `ワークフローを開始できません（${(e as Error).message}）。` }),
  });

  if (!open) {
    return (
      <button
        className="btn"
        onClick={() => setOpen(true)}
        disabled={workflows.isLoading}
        title="有効なワークフローにこの帳票を投入します"
      >
        ワークフローで処理
      </button>
    );
  }

  return (
    <div className="run-wf">
      {workflows.isError ? (
        // 取得失敗を「有効なワークフローがありません」と混同させない（誤って再作成に誘導しない）
        <span className="sub">
          ワークフロー一覧を取得できません。
          <button className="btn sm ghost" onClick={() => workflows.refetch()}>
            再試行
          </button>
        </span>
      ) : active.length === 0 ? (
        <span className="sub">
          有効なワークフローがありません（
          <a href="/workflows">ワークフロー</a> で作成・有効化してください）。
        </span>
      ) : (
        <>
          <select
            value={effective}
            onChange={(e) => setWfId(e.target.value)}
            disabled={run.isPending}
            aria-label="ワークフロー"
          >
            {active.map((w) => (
              <option key={w.id} value={w.id}>
                {w.name}（v{w.version}）
              </option>
            ))}
          </select>
          <button className="btn primary" onClick={() => run.mutate()} disabled={run.isPending || !effective}>
            {run.isPending ? "開始中…" : "実行"}
          </button>
        </>
      )}
      <button className="btn ghost" onClick={() => setOpen(false)} disabled={run.isPending}>
        閉じる
      </button>
    </div>
  );
}
