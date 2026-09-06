"use client";

// ページ画像の署名 URL を取る共有フック（DocViewer / RegionCanvas）。
//
// **ページ変更時に url を即 null へ戻す**のがこのフックの要点。DocViewer の元実装は
// 新しい URL が届くまで旧ページの画像を表示し続けており、閲覧するだけなら
// 「一瞬前のページが残る」程度で済んでいた。しかし領域を描く画面では、旧ページの
// 画像の上にドラッグできてしまい、**別のページの座標として矩形が保存される**。
// 切り出しに際してその挙動は継承しない。

import { useEffect, useState } from "react";

import { api } from "@/lib/api";

export function usePageImage(documentId: string, pageNo: number) {
  const [url, setUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let active = true;
    let retried = false;
    setUrl(null); // ★ 旧ページの画像を残さない
    setFailed(false);

    const load = () => {
      api
        .pageImage(documentId, pageNo)
        .then((r) => {
          if (active) setUrl(r.url);
        })
        .catch(() => {
          if (!active) return;
          // 署名 URL の期限切れ・403 は 1 回だけ取り直す（長時間開いたままの編集画面）。
          // 無限に再試行すると権限が無い利用者でリクエストが積み上がる。
          if (!retried) {
            retried = true;
            setTimeout(() => active && load(), 400);
            return;
          }
          setFailed(true);
        });
    };
    load();
    return () => {
      active = false;
    };
  }, [documentId, pageNo]);

  return { url, failed };
}
