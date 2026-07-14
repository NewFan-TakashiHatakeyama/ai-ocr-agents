"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";

import { api } from "@/lib/api";

// SCR-02 ドキュメント一覧（§8.1）。
export default function DocumentsPage() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["documents"],
    queryFn: () => api.listDocuments(),
  });

  return (
    <div className="container">
      <h1>ドキュメント一覧</h1>
      {isLoading && <p>読み込み中…</p>}
      {error && <p style={{ color: "var(--low)" }}>取得に失敗しました。トークン/接続を確認してください。</p>}
      <table className="list">
        <thead>
          <tr>
            <th>ドキュメント</th>
            <th>種別</th>
            <th>ページ</th>
            <th>状態</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {data?.items.map((d) => (
            <tr key={d.document_id}>
              <td>{d.document_id}</td>
              <td>{d.doc_type ?? "-"}</td>
              <td>{d.page_count ?? "-"}</td>
              <td>{d.status}</td>
              <td>
                <Link href={`/documents/${d.document_id}`}>検証</Link>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
