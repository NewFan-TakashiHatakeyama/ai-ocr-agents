"use client";

import { useQuery } from "@tanstack/react-query";

import { AppShell } from "@/components/AppShell";
import { statusChip } from "@/lib/fields";
import { ApiError, api } from "@/lib/api";

// SCR-04 ダッシュボード（§12.1）。KPI は可観測性メトリクスと1:1。admin。
const STATUS_COLOR: Record<string, string> = {
  confirmed: "var(--green)",
  exported: "var(--blue)",
  needs_review: "var(--amber)",
  in_review: "var(--blue)",
  processing: "var(--violet)",
  failed: "var(--red)",
};

function AdminDenied() {
  return (
    <AppShell active="dashboard">
      <div className="topbar">
        <span className="ttl">ダッシュボード</span>
      </div>
      <div className="access-denied">
        <div style={{ fontSize: 30 }}>🔒</div>
        <h3>権限がありません</h3>
        <p>この画面は管理者（admin）のみ利用できます。</p>
      </div>
    </AppShell>
  );
}

export default function DashboardPage() {
  const { data, error, isLoading } = useQuery({ queryKey: ["metrics"], queryFn: () => api.metrics() });

  if (error instanceof ApiError && error.status === 403) return <AdminDenied />;

  const totalRuns = data ? Object.values(data.status_counts).reduce((a, b) => a + b, 0) : 0;

  return (
    <AppShell active="dashboard">
      <div className="topbar">
        <span className="ttl">ダッシュボード</span>
        <span className="spacer" />
        <span className="filter">期間: 全期間</span>
        <button className="btn sm" disabled title="準備中">
          CSVダウンロード
        </button>
      </div>

      {isLoading && <p className="page">読み込み中…</p>}

      {data && (
        <>
          <div className="kpis">
            <div className="kpi">
              <div className="k">STP率（自動確定）</div>
              <div className="v">{(data.stp_rate * 100).toFixed(1)}%</div>
              <div className="d" style={{ color: "var(--ink3)" }}>修正なしで確定した割合</div>
            </div>
            <div className="kpi">
              <div className="k">フィールド精度（サンプル監査）</div>
              <div className="v">{data.field_accuracy_sampled != null ? `${(data.field_accuracy_sampled * 100).toFixed(1)}%` : "—"}</div>
              <div className="d" style={{ color: "var(--ink3)" }}>週次サンプル監査（未計測）</div>
            </div>
            <div className="kpi">
              <div className="k">LLMコスト</div>
              <div className="v">{data.llm_cost_jpy_total != null ? `¥${data.llm_cost_jpy_total.toLocaleString()}` : "—"}</div>
              <div className="d" style={{ color: "var(--ink3)" }}>トークン計測（未整備）</div>
            </div>
            <div className="kpi">
              <div className="k">総ドキュメント</div>
              <div className="v">{data.total_documents.toLocaleString()}</div>
              <div className="d" style={{ color: "var(--ink3)" }}>累計</div>
            </div>
          </div>

          <div className="db-row">
            <div className="card">
              <h5>処理内訳（Run {totalRuns.toLocaleString()}件）</h5>
              <div className="statbars">
                {Object.entries(data.status_counts)
                  .sort((a, b) => b[1] - a[1])
                  .map(([st, n]) => {
                    const { label } = statusChip(st);
                    return (
                      <div key={st} className="statbar">
                        <span>{label}</span>
                        <span className="track">
                          <i style={{ width: `${totalRuns ? (n / totalRuns) * 100 : 0}%`, background: STATUS_COLOR[st] ?? "var(--ink3)" }} />
                        </span>
                        <span className="mono" style={{ textAlign: "right" }}>{n}</span>
                      </div>
                    );
                  })}
                {totalRuns === 0 && <p className="sub">まだ Run がありません。</p>}
              </div>
            </div>

            <div className="card">
              <h5>学習の効き（修正の再利用）</h5>
              <div className="lstat">
                <span>累計の人手修正（学習ソース）</span>
                <b>{data.corrections_total.toLocaleString()}</b>
              </div>
              <div className="lstat">
                <span>登録済み修正メモリ</span>
                <b>{data.memories_total.toLocaleString()}</b>
              </div>
              <div className="lstat">
                <span>有効なルール</span>
                <b>{data.active_rules.toLocaleString()}</b>
              </div>
              <div className="lstat">
                <span>新規ルール（承認待ち）</span>
                <b style={{ color: data.pending_rules ? "var(--amber-ink)" : undefined }}>{data.pending_rules}</b>
              </div>
              <p className="sub" style={{ marginTop: 8 }}>
                学習が価値になっていることを示す差別化ウィジェット（§5.8）。承認待ちは「ルール管理」で確認できます。
              </p>
            </div>
          </div>
        </>
      )}
    </AppShell>
  );
}
