"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { type ReactNode, useEffect, useState } from "react";

import { SignIn } from "@/components/SignIn";
import { StatusChip } from "@/components/StatusChip";
import { api } from "@/lib/api";

// §4 共通レイアウト: サイドナビ216px（フロストガラス）＋ main。
// 一覧系画面で使用。検証画面(SCR-03)は集中のためフルスクリーン（本シェル非使用）。

type Principal = { sub: string; role: string };

function decodePrincipal(token: string | undefined | null): Principal {
  try {
    if (!token) return { sub: "guest", role: "viewer" };
    const p = JSON.parse(atob(token.split(".")[1]));
    return { sub: p.sub ?? "user", role: p.role ?? "viewer" };
  } catch {
    return { sub: "user", role: "viewer" };
  }
}

// localStorage を render 中に読むと SSR と初回クライアントレンダで結果が変わり
// hydration mismatch になる（サーバは env トークン、クライアントは localStorage を見るため）。
// 初期値は env 由来の決定論的な値にし、localStorage はマウント後にのみ反映する。
type Session = { me: Principal; hasToken: boolean; ready: boolean };

function usePrincipal(): Session {
  const envToken = process.env.NEXT_PUBLIC_DEV_TOKEN;
  const [me, setMe] = useState<Principal>(() => decodePrincipal(envToken));
  // ready=false の間は判定を出さない。localStorage を見る前に「トークン無し」と
  // 決めるとサインイン画面が一瞬ちらつく（SSR と初回クライアントで結果が違うため）。
  const [state, setState] = useState<{ hasToken: boolean; ready: boolean }>({
    hasToken: Boolean(envToken),
    ready: false,
  });
  useEffect(() => {
    const t = window.localStorage.getItem("nf_token") || envToken;
    setMe(decodePrincipal(t));
    setState({ hasToken: Boolean(t), ready: true });
  }, [envToken]);
  return { me, ...state };
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
