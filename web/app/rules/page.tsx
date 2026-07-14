"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { AppShell } from "@/components/AppShell";
import { ApiError, api } from "@/lib/api";
import { useToasts } from "@/lib/toast";
import type { RuleDto } from "@/lib/types";

// SCR-05 ルール管理（§5.8.4）。AI が学んだルール（draft）を検証レポート付きで承認/退役。
const RULE_STATUS: Record<string, { cls: string; label: string }> = {
  draft: { cls: "st-review", label: "承認待ち" },
  validating: { cls: "st-processing", label: "検証中" },
  active: { cls: "st-confirmed", label: "有効" },
  retired: { cls: "st-failed", label: "退役" },
};

function reportText(r: RuleDto): string {
  const rep = r.validation_report;
  if (!rep) return "—";
  const pct = Math.round((rep.reproduction_rate ?? 0) * 100);
  return `再現 ${pct}% · 回帰 ${rep.regressions ?? 0}件`;
}

function AdminDenied() {
  return (
    <AppShell active="rules">
      <div className="topbar">
        <span className="ttl">ルール管理</span>
      </div>
      <div className="access-denied">
        <div style={{ fontSize: 30 }}>🔒</div>
        <h3>権限がありません</h3>
        <p>この画面は管理者（admin）のみ利用できます。</p>
      </div>
    </AppShell>
  );
}

export default function RulesPage() {
  const qc = useQueryClient();
  const push = useToasts((s) => s.push);
  const { data, error } = useQuery({ queryKey: ["rules"], queryFn: () => api.listRules() });
  const [selected, setSelected] = useState<string | null>(null);

  const patch = useMutation({
    mutationFn: ({ id, status }: { id: string; status: string }) => api.patchRule(id, status),
    onSuccess: (r) => {
      push({ kind: "ok", message: `ルールを${r.status === "active" ? "有効化" : "退役"}しました（${r.id}）。` });
      qc.invalidateQueries({ queryKey: ["rules"] });
      qc.invalidateQueries({ queryKey: ["metrics"] });
    },
    onError: (e) => {
      const msg =
        e instanceof ApiError && e.status === 409
          ? "検証未達のため有効化できません（再現率≥90%・回帰0件が必要）。"
          : `操作に失敗しました（${(e as Error).message}）。`;
      push({ kind: "warn", message: msg });
    },
  });

  if (error instanceof ApiError && error.status === 403) return <AdminDenied />;

  const rules = data?.items ?? [];
  const pending = rules.filter((r) => r.status === "draft").length;
  const sel = rules.find((r) => r.id === selected) ?? rules[0];

  return (
    <AppShell active="rules">
      <div className="topbar">
        <span className="ttl">ルール管理</span>
        {pending > 0 && <span className="chip st-review">承認待ち {pending}</span>}
        <span className="spacer" />
      </div>

      <div style={{ padding: "14px 22px", display: "grid", gridTemplateColumns: "1.55fr 1fr", gap: 14 }}>
        <div style={{ overflow: "auto" }}>
          <table className="dtable" style={{ border: "1px solid var(--line)", borderRadius: 12, overflow: "hidden" }}>
            <thead>
              <tr>
                <th>ルール</th>
                <th>種別</th>
                <th>対象</th>
                <th>状態</th>
                <th>検証レポート</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {rules.map((r) => {
                const st = RULE_STATUS[r.status] ?? { cls: "st-uploaded", label: r.status };
                return (
                  <tr key={r.id} className={sel?.id === r.id ? "" : ""} onClick={() => setSelected(r.id)}>
                    <td>
                      <b>{(r.rule_json.description as string) ?? r.field_name ?? r.rule_type}</b>
                      <div className="sub">
                        {r.id.slice(0, 10)}… · {r.created_by}生成 · 根拠{r.source_correction_ids.length}件
                      </div>
                    </td>
                    <td>
                      <span className={`rtype rt-${r.rule_type}`}>{r.rule_type}</span>
                    </td>
                    <td className="sub">
                      {[r.doc_type ?? "すべて", r.supplier_key ?? "すべて", r.field_name ?? "—"].join(" · ")}
                    </td>
                    <td>
                      <span className={`chip ${st.cls}`}>{st.label}</span>
                    </td>
                    <td className="sub">
                      {r.activatable ? <b className="up">{reportText(r)}</b> : reportText(r)}
                    </td>
                    <td onClick={(e) => e.stopPropagation()}>
                      {r.status === "active" ? (
                        <button className="btn sm" onClick={() => patch.mutate({ id: r.id, status: "retired" })}>
                          退役
                        </button>
                      ) : r.status === "retired" ? (
                        <span className="sub">—</span>
                      ) : (
                        <button
                          className="btn sm primary"
                          disabled={!r.activatable || patch.isPending}
                          title={r.activatable ? "" : "検証未達（再現率≥90%・回帰0件が必要）"}
                          onClick={() => patch.mutate({ id: r.id, status: "active" })}
                        >
                          有効化
                        </button>
                      )}
                    </td>
                  </tr>
                );
              })}
              {rules.length === 0 && (
                <tr>
                  <td colSpan={6} className="sub">
                    ルールはまだありません。修正学習エージェントが抽出すると承認待ちで表示されます。
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>

        <div className="card">
          <h5>選択中のルール — {sel ? `${sel.id.slice(0, 10)}…` : "（未選択）"}</h5>
          {sel ? (
            <>
              <div className="rulejson">{JSON.stringify({ rule_type: sel.rule_type, field: sel.field_name, ...sel.rule_json }, null, 2)}</div>
              <p className="sub" style={{ marginTop: 10 }}>
                根拠修正ログ {sel.source_correction_ids.length}件・dry-runプレビュー・監査履歴を表示（有効化/退役は audit_logs 記録, §11）。
                有効化条件: 再現率≥90% かつ 回帰0件（§5.8.4）。
              </p>
            </>
          ) : (
            <p className="sub">左の一覧から選択してください。</p>
          )}
        </div>
      </div>
    </AppShell>
  );
}
