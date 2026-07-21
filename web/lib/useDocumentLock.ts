"use client";

import { useEffect, useState } from "react";

import { api } from "./api";
import type { LockStatus } from "./types";

// §8.2 検証画面ソフトロック: マウントで取得、30秒ごとに heartbeat、離脱で解放。
// 他者が保持中なら readOnly=true（UI を読取専用にしバナー表示）。助言的ロックのため
// エラーにはせず、保持者が離脱/失効すれば次の heartbeat で自動的に取得できる。
export function useDocumentLock(documentId: string) {
  const [status, setStatus] = useState<LockStatus | null>(null);
  const [remaining, setRemaining] = useState(0);

  useEffect(() => {
    let active = true;
    const beat = async () => {
      try {
        const s = await api.acquireLock(documentId);
        if (!active) return;
        setStatus(s);
        setRemaining(s.remaining_sec);
      } catch {
        /* 助言的ロック: 失敗は無視（編集は妨げない） */
      }
    };
    void beat();
    const hb = setInterval(beat, 30_000);
    return () => {
      active = false;
      clearInterval(hb);
      // 保持者本人のときだけサーバ側で解放される（非保持者呼び出しは no-op）
      void api.releaseLock(documentId).catch(() => {});
    };
  }, [documentId]);

  // 他者ロック中の残り時間を 1 秒ごとにカウントダウン表示
  useEffect(() => {
    if (!status?.locked) return;
    const t = setInterval(() => setRemaining((r) => Math.max(0, r - 1)), 1000);
    return () => clearInterval(t);
  }, [status]);

  const readOnly = !!status && status.locked && !status.held_by_me;
  return { status, readOnly, remaining, holder: status?.holder ?? null };
}

export function fmtRemaining(sec: number): string {
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}
