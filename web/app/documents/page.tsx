"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useMemo, useRef, useState } from "react";

import { AppShell } from "@/components/AppShell";
import { StatusChip } from "@/components/StatusChip";
import { api } from "@/lib/api";
import { useToasts } from "@/lib/toast";

// 取り込める帳票の種類（ingest が受ける MIME に合わせる）
const ACCEPT = "application/pdf,image/png,image/jpeg,image/tiff,.pdf,.png,.jpg,.jpeg,.tif,.tiff";

// SCR-02 ドキュメント一覧 / レビューキュー（§8.1 / §8.5）。2タブ構成。
function DocumentsInner() {
  const router = useRouter();
  const params = useSearchParams();
  const qc = useQueryClient();
  const push = useToasts((s) => s.push);
  const tab = params.get("tab") === "queue" ? "queue" : "all";
  const [q, setQ] = useState("");
  const [dragOver, setDragOver] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const docs = useQuery({ queryKey: ["documents"], queryFn: () => api.listDocuments() });
  const queue = useQuery({ queryKey: ["review-queue"], queryFn: () => api.reviewQueue() });
  const queueCount = queue.data?.items.length ?? 0;

  // アップロード → 一覧更新 → その帳票ページへ（そこで「抽出を開始」できる）
  const upload = useMutation({
    mutationFn: (file: File) => api.uploadDocument(file),
    onSuccess: (created) => {
      qc.invalidateQueries({ queryKey: ["documents"] });
      push({ kind: "ok", message: "アップロードしました。抽出を開始できます。" });
      router.push(`/documents/${created.document_id}`);
    },
    onError: (e) =>
      push({ kind: "warn", message: `アップロードに失敗しました（${(e as Error).message}）。` }),
  });

  function onFiles(files: FileList | null) {
    const file = files?.[0];
    if (file) upload.mutate(file);
  }

  const filtered = useMemo(() => {
    const items = docs.data?.items ?? [];
    const kw = q.trim().toLowerCase();
    if (!kw) return items;
    return items.filter((d) =>
      [d.document_id, d.doc_type, d.external_ref].some((v) => v?.toLowerCase().includes(kw)),
    );
  }, [docs.data, q]);

  function setTab(next: "all" | "queue") {
    router.replace(next === "queue" ? "/documents?tab=queue" : "/documents");
  }

  function prioClass(p: number) {
    return p >= 70 ? "p1" : p >= 40 ? "p2" : "p3";
  }

  return (
    <AppShell active="documents">
      <div className="topbar">
        <span className="ttl">ドキュメント</span>
        <span className="spacer" />
        <span className="inp" style={{ width: 260 }}>
          🔍
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="帳票ID・取引先・external_ref で検索"
            aria-label="検索"
          />
        </span>
        <input
          ref={fileRef}
          type="file"
          accept={ACCEPT}
          hidden
          onChange={(e) => {
            onFiles(e.target.files);
            e.target.value = ""; // 同じファイルを続けて選べるように
          }}
        />
        <button
          className="btn sm grad"
          onClick={() => fileRef.current?.click()}
          disabled={upload.isPending}
        >
          {upload.isPending ? "アップロード中…" : "＋ アップロード"}
        </button>
      </div>

      <div className="list-tools">
        <div className="tab2" role="tablist">
          <button className={tab === "all" ? "on" : ""} onClick={() => setTab("all")} role="tab" aria-selected={tab === "all"}>
            すべて
          </button>
          <button className={tab === "queue" ? "on" : ""} onClick={() => setTab("queue")} role="tab" aria-selected={tab === "queue"}>
            レビューキュー{queueCount > 0 && `（${queueCount}）`}
          </button>
        </div>
      </div>

      <div
        className={`doc-dropzone${dragOver ? " over" : ""}`}
        style={{ overflow: "auto" }}
        onDragOver={(e) => {
          if (tab !== "all") return;
          e.preventDefault();
          setDragOver(true);
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragOver(false);
          if (tab === "all") onFiles(e.dataTransfer.files);
        }}
      >
        {dragOver && <div className="doc-droplabel">ここにドロップして帳票をアップロード</div>}
        {tab === "all" ? (
          <>
            {docs.isLoading && <p className="page">読み込み中…</p>}
            {docs.error && (
              <p className="page" style={{ color: "var(--red)" }}>
                取得に失敗しました。トークン/接続を確認してください。
              </p>
            )}
            <table className="dtable">
              <thead>
                <tr>
                  <th>帳票</th>
                  <th>種別</th>
                  <th>ページ</th>
                  <th>状態</th>
                  <th>external_ref</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((d) => (
                  <tr key={d.document_id} onClick={() => router.push(`/documents/${d.document_id}`)}>
                    <td>
                      <span className="docname">{d.document_id}</span>
                    </td>
                    <td>{d.doc_type ?? "—"}</td>
                    <td className="mono">{d.page_count ?? "—"}</td>
                    <td>
                      <StatusChip status={d.status} />
                    </td>
                    <td className="sub">{d.external_ref ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {docs.data && filtered.length === 0 && (
              <div className="empty">
                <div className="emoji">🗂️</div>
                <h3>該当するドキュメントがありません</h3>
                <p>帳票をアップロードするか、ここにドラッグ&ドロップしてください。</p>
                <button className="btn grad" style={{ marginTop: 10 }} onClick={() => fileRef.current?.click()}>
                  ＋ 帳票をアップロード
                </button>
              </div>
            )}
          </>
        ) : (
          <>
            {queue.isLoading && <p className="page">読み込み中…</p>}
            {queueCount > 0 ? (
              <table className="dtable">
                <thead>
                  <tr>
                    <th>優先度</th>
                    <th>帳票</th>
                    <th>要確認</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {[...(queue.data?.items ?? [])]
                    .sort((a, b) => b.priority - a.priority)
                    .map((it) => (
                      <tr key={it.run_id} onClick={() => router.push(`/documents/${it.document_id}`)}>
                        <td>
                          <span className={`prio ${prioClass(it.priority)}`}>
                            {Math.round(it.priority)}
                          </span>
                        </td>
                        <td>
                          <span className="docname">{it.document_id}</span>
                        </td>
                        <td>
                          <span className="chip st-review">要確認 {it.pending}</span>
                        </td>
                        <td>
                          <span className="sub">Enter / クリックで検証 →</span>
                        </td>
                      </tr>
                    ))}
                </tbody>
              </table>
            ) : (
              <div className="empty">
                <div className="emoji">🎉</div>
                <h3>レビュー待ちはありません</h3>
                <p>要確認のドキュメントが届くとここに並びます。</p>
              </div>
            )}
          </>
        )}
      </div>
    </AppShell>
  );
}

// useSearchParams は Suspense 境界内で使う（Next 15）。
export default function DocumentsPage() {
  return (
    <Suspense fallback={<div className="page">読み込み中…</div>}>
      <DocumentsInner />
    </Suspense>
  );
}
