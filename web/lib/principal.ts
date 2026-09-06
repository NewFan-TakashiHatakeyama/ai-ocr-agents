"use client";

// ログイン中の利用者（JWT のクレーム）。§6.1 / §11 の RBAC は階層ランク。
//
// 権限の判断はサーバ（gateway の require_role）が正で、ここは UI の出し分けだけに使う。
// トークンは利用者が差し替えられるので、これをセキュリティ境界にしてはいけない。

import { useEffect, useState } from "react";

export type Principal = { sub: string; role: string };

// gateway の auth.ROLE_RANK と一致させること（ずれると UI とサーバの判断が食い違う）
const ROLE_RANK: Record<string, number> = {
  viewer: 1,
  uploader: 2,
  api: 2,
  reviewer: 3,
  admin: 4,
};

export function decodePrincipal(token: string | undefined | null): Principal {
  try {
    if (!token) return { sub: "guest", role: "viewer" };
    const p = JSON.parse(atob(token.split(".")[1]));
    return { sub: p.sub ?? "user", role: p.role ?? "viewer" };
  } catch {
    return { sub: "user", role: "viewer" };
  }
}

export function hasRole(role: string, min: string): boolean {
  return (ROLE_RANK[role] ?? 0) >= (ROLE_RANK[min] ?? 99);
}

export type Session = { me: Principal; hasToken: boolean; ready: boolean };

/** localStorage の nf_token（無ければ env）から Principal を解決する。
 *
 * localStorage を render 中に読むと SSR と初回クライアントレンダで結果が変わり
 * hydration mismatch になる。初期値は env 由来の決定論的な値にし、
 * localStorage はマウント後にのみ反映する。 */
export function usePrincipal(): Session {
  const envToken = process.env.NEXT_PUBLIC_DEV_TOKEN;
  const [me, setMe] = useState<Principal>(() => decodePrincipal(envToken));
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
