"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";

import { api } from "@/lib/api";

// SCR-03 レビューキュー（§8.5）。優先度順。
export default function ReviewQueuePage() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["review-queue"],
    queryFn: () => api.reviewQueue(),
  });

  return (
    <div className="container">
      <h1>レビューキュー</h1>
      {isLoading && <p>読み込み中…</p>}
      {error && <p style={{ color: "var(--low)" }}>取得に失敗しました。</p>}
      <table className="list">
        <thead>
          <tr>
            <th>ドキュメント</th>
            <th>要確認</th>
            <th>優先度</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {data?.items.map((it) => (
            <tr key={it.run_id}>
              <td>{it.document_id}</td>
              <td>{it.pending}</td>
              <td>{it.priority.toFixed(1)}</td>
              <td>
                <Link href={`/documents/${it.document_id}`}>検証する</Link>
              </td>
            </tr>
          ))}
          {data && data.items.length === 0 && (
            <tr>
              <td colSpan={4}>レビュー待ちはありません。</td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
