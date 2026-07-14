"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import type { ReactNode } from "react";

import { StatusChip } from "@/components/StatusChip";
import { api } from "@/lib/api";

// §4 共通レイアウト: サイドナビ216px（フロストガラス）＋ main。
// 一覧系画面で使用。検証画面(SCR-03)は集中のためフルスクリーン（本シェル非使用）。

function principal(): { sub: string; role: string } {
  try {
    const t =
      (typeof window !== "undefined" && window.localStorage.getItem("nf_token")) ||
      process.env.NEXT_PUBLIC_DEV_TOKEN;
    if (!t) return { sub: "guest", role: "viewer" };
    const p = JSON.parse(atob(t.split(".")[1]));
    return { sub: p.sub ?? "user", role: p.role ?? "viewer" };
  } catch {
    return { sub: "user", role: "viewer" };
  }
}

const ICON = {
  chat: "M21 12a8 8 0 1 1-4-6.9M21 3v6h-6",
  doc: "M4 3h16v18H4zM8 8h8M8 12h8M8 16h5",
  dash: "M4 19V5m0 14h16M8 15v-4m4 4V8m4 7v-6",
  rule: "M4 6h16M4 12h10M4 18h7",
  schema: "M4 4h16v6H4zM4 14h7v6H4zM14 14h6v6h-6z",
};

function NavIcon({ d }: { d: string }) {
  return (
    <svg className="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}>
      {d.split("M").filter(Boolean).map((seg, i) => (
        <path key={i} d={`M${seg}`} />
      ))}
    </svg>
  );
}

export function AppShell({ active, children }: { active: string; children: ReactNode }) {
  const me = principal();
  const isAdmin = me.role === "admin";
  const { data } = useQuery({ queryKey: ["documents"], queryFn: () => api.listDocuments() });
  const recent = (data?.items ?? []).slice(0, 20);

  return (
    <div className="app">
      <aside className="side">
        <div className="side-logo">
          <span className="logo-mark" />
          NewFan OCR
        </div>
        <Link href="/chat" className="nav-new">
          ＋ 新規
        </Link>

        <Link href="/chat" className={`nav-item${active === "chat" ? " on" : ""}`}>
          <NavIcon d={ICON.chat} />
          チャット
        </Link>
        <Link href="/documents" className={`nav-item${active === "documents" ? " on" : ""}`}>
          <NavIcon d={ICON.doc} />
          ドキュメント
        </Link>

        <div className="nav-sec">管理{!isAdmin && "（admin）"}</div>
        {(["dash", "rule", "schema"] as const).map((k) => {
          const meta = {
            dash: { label: "ダッシュボード", href: "/dashboard", key: "dashboard" },
            rule: { label: "ルール管理", href: "/rules", key: "rules" },
            schema: { label: "スキーマ管理", href: "/schemas", key: "schemas" },
          }[k];
          if (!isAdmin) {
            return (
              <div key={k} className="nav-item disabled" title={`${meta.label}（admin 専用）`}>
                <NavIcon d={ICON[k]} />
                {meta.label}
                <span className="sub" style={{ marginLeft: "auto" }}>admin</span>
              </div>
            );
          }
          return (
            <Link
              key={k}
              href={meta.href}
              className={`nav-item${active === meta.key ? " on" : ""}`}
            >
              <NavIcon d={ICON[k]} />
              {meta.label}
            </Link>
          );
        })}

        <div className="nav-sec">最近のドキュメント</div>
        <div className="side-recent">
          {recent.map((d) => (
            <Link key={d.document_id} href={`/documents/${d.document_id}`} className="recent-item">
              <span className="rn">{d.document_id}</span>
              <StatusChip status={d.status} />
            </Link>
          ))}
          {recent.length === 0 && <div className="recent-item">（履歴なし）</div>}
        </div>

        <div className="side-user">
          <span className="avatar">{me.sub.slice(0, 1).toUpperCase()}</span>
          <div>
            <b style={{ fontSize: 11.5 }}>{me.sub}</b>
            <div style={{ fontSize: 10, color: "var(--ink3)" }}>{me.role}</div>
          </div>
        </div>
      </aside>
      <div className="main">{children}</div>
    </div>
  );
}
