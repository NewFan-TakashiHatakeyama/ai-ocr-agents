"use client";

// SCR-07 ワークフロー一覧（§16 P7）。作成 → エディタへ。
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { AppShell } from "@/components/AppShell";
import { StatusChip } from "@/components/StatusChip";
import { ApiError, api } from "@/lib/api";
import { useToasts } from "@/lib/toast";

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
        {(data?.items ?? []).map((w) => (
          <Link key={w.id} href={`/workflows/${w.id}`} className="wf-card">
            <div className="wf-card-head">
              <span className="wf-name">{w.name}</span>
              <StatusChip status={w.status} />
            </div>
            <div className="wf-card-sub">
              v{w.version}
              {w.updated_at ? ` ・ 更新 ${new Date(w.updated_at).toLocaleString("ja-JP")}` : ""}
              <span className="sub" style={{ marginLeft: 8 }}>
                {w.id}
              </span>
            </div>
          </Link>
        ))}
        {data && data.items.length === 0 && (
          <div className="empty">
            まだワークフローがありません。「＋ 新規作成」から始めてください。
          </div>
        )}
      </div>
    </AppShell>
  );
}
