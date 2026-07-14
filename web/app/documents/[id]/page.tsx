"use client";

import { useQuery } from "@tanstack/react-query";
import { use } from "react";

import { ConfirmBar } from "@/components/ConfirmBar";
import { DocViewer } from "@/components/DocViewer";
import { FieldPanel } from "@/components/FieldPanel";
import { api } from "@/lib/api";

// SCR-03 検証画面（§8.2 左=ページ画像ビューア / 右=フィールドパネル）。
export default function VerifyPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["result", id],
    queryFn: () => api.getResult(id),
  });

  if (isLoading) return <div className="container">読み込み中…</div>;
  if (error || !data)
    return <div className="container">結果の取得に失敗しました。</div>;

  // 選択フィールドのページを優先表示（なければ 1 ページ目）
  const pageNo = data.fields.find((f) => f.bbox)?.page ?? 1;

  return (
    <div>
      <div className="verify">
        <DocViewer documentId={id} fields={data.fields} pageNo={pageNo} />
        <FieldPanel fields={data.fields} />
      </div>
      <ConfirmBar result={data} onDone={() => refetch()} />
    </div>
  );
}
