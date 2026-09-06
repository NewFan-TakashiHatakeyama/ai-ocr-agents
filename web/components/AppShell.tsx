"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { type ReactNode, useEffect, useState } from "react";

import { SignIn } from "@/components/SignIn";
import { StatusChip } from "@/components/StatusChip";
import { api } from "@/lib/api";
import { usePrincipal } from "@/lib/principal";

// §4 共通レイアウト: サイドナビ216px（フロストガラス）＋ main。
// 一覧系画面で使用。検証画面(SCR-03)は集中のためフルスクリーン（本シェル非使用）。

// Principal の解決は lib/principal.ts に集約する（検証画面は AppShell を使わないが
// ロールで出し分けたい導線があるため、シェル内に閉じ込めない）。
// ready=false の間は判定を出さない。localStorage を見る前に「トークン無し」と
// 決めるとサインイン画面が一瞬ちらつく。

const ICON = {
  chat: "M21 12a8 8 0 1 1-4-6.9M21 3v6h-6",
  doc: "M4 3h16v18H4zM8 8h8M8 12h8M8 16h5",
  dash: "M4 19V5m0 14h16M8 15v-4m4 4V8m4 7v-6",
  rule: "M4 6h16M4 12h10M4 18h7",
  schema: "M4 4h16v6H4zM4 14h7v6H4zM14 14h6v6h-6z",
  flow: "M3 5h6v5H3zM15 14h6v5h-6zM9 7.5h9M18 7.5V14M6 10v6.5h9",
  conn: "M9 12a3 3 0 0 1 3-3h1M15 12a3 3 0 0 1-3 3h-1M7 12H4M20 12h-3",
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
  const { me, hasToken, ready } = usePrincipal();
  const [signedIn, setSignedIn] = useState(false);
  const isAdmin = me.role === "admin";
  const needsSignIn = ready && !hasToken && !signedIn;

  // トークンが無ければ 403 になるだけなので API を叩かない（enabled）。
  // 早期 return はフックを全て呼び終えてから行う。フックより前に return すると
  // レンダー間でフック数が変わり React error #300 で画面が落ちる（実際に踏んだ）。
  const { data } = useQuery({
    queryKey: ["documents"],
    queryFn: () => api.listDocuments(),
    enabled: !needsSignIn,
  });
  const recent = (data?.items ?? []).slice(0, 20);

  // トークンが無ければ先に投入してもらう。以前は DevTools で localStorage を
  // 手打ちする以外に入れる手段が無かった。
  if (needsSignIn) {
    return <SignIn onSignedIn={() => { setSignedIn(true); location.reload(); }} />;
  }

  return (
    <div className="app">
      <aside className="side">
        {/* next/image を避けているのは standalone 出力での画像最適化に sharp が要るため。
            固定サイズのロゴ 1 枚のために実行時依存を増やさない。 */}
        <Link href="/chat" className="side-logo" aria-label="NewFan AI OCR">
          <img src="/logo-full.png" alt="NewFan AI OCR" width={908} height={151} />
        </Link>
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
        {(["flow", "conn", "dash", "rule", "schema"] as const).map((k) => {
          const meta = {
            flow: { label: "ワークフロー", href: "/workflows", key: "workflows" },
            conn: { label: "接続管理", href: "/connections", key: "connections" },
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
