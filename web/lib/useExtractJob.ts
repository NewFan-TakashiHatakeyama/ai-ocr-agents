"use client";

// 抽出ジョブの完了待ち（§6.3 の polling 契約）を共有する。
//
// 元は ExtractStart.tsx の中にあった。テンプレート化直後の「この帳票を再抽出」でも
// 同じ待ち方が要る。締切・dead 終端・恒久エラーでの打ち切りは、どちらか一方だけに
// 実装されていると「片方の導線だけ無限ループする」ことになるので共有する。

import { useCallback, useEffect, useRef } from "react";

import { ApiError, api } from "@/lib/api";

const DEADLINE_MS = 3 * 60_000; // 3分で打ち切り（キュー滞留/ワーカー停止対策）
const INTERVAL_MS = 1500;

export function useExtractJob() {
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const poll = useCallback(
    (jobId: string, handlers: { onDone: () => void; onFail: (message: string) => void }) => {
      const startedAt = Date.now();
      const fail = (message: string) => {
        if (alive.current) handlers.onFail(message);
      };
      const tick = async () => {
        if (!alive.current) return;
        if (Date.now() - startedAt > DEADLINE_MS) {
          fail("抽出がタイムアウトしました。時間をおいて再試行してください。");
          return;
        }
        try {
          const j = await api.getJob(jobId);
          if (!alive.current) return;
          if (j.status === "succeeded") {
            handlers.onDone();
            return;
          }
          // dead も終端（再配信枯渇）。failed と同じく失敗として止める
          if (j.status === "failed" || j.status === "dead") {
            fail("抽出に失敗しました。時間をおいて再試行してください。");
            return;
          }
        } catch (e) {
          // 一時的な 5xx/ネットワークは継続。ジョブ未検出(E1001)や 4xx 恒久エラーは打ち切る
          if (e instanceof ApiError && (e.code === "E1001" || (e.status >= 400 && e.status < 500))) {
            fail("抽出状況を取得できませんでした。時間をおいて再試行してください。");
            return;
          }
        }
        if (alive.current) setTimeout(tick, INTERVAL_MS);
      };
      void tick();
    },
    [],
  );

  return { poll };
}
