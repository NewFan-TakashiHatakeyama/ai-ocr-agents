"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useMemo, useRef, useState } from "react";

import { AppShell } from "@/components/AppShell";
import { DeleteDocument } from "@/components/DeleteDocument";
import { StatusChip } from "@/components/StatusChip";
import { api } from "@/lib/api";
import {
  countRunning,
  latestSchemaId,
  partitionFiles,
  partitionSelection,
  summarizeBatch,
  summarizeUploads,
  type UploadTally,
} from "@/lib/bulk";
import { hasRole, usePrincipal } from "@/lib/principal";
import { useToasts } from "@/lib/toast";
import { newUuid } from "@/lib/uuid";

// 取り込める帳票の種類（ingest が受ける MIME に合わせる。lib/bulk.ts の選別と対）
const ACCEPT = "application/pdf,image/png,image/jpeg,image/tiff,.pdf,.png,.jpg,.jpeg,.tif,.tiff";

// 抽出が動いている帳票が一覧にある間の再取得間隔（設計 bulk-processing D8）
const RUNNING_REFETCH_MS = 5000;

// SCR-02 ドキュメント一覧 / レビューキュー（§8.1 / §8.5）。2タブ構成。
function DocumentsInner() {
  const router = useRouter();
  const params = useSearchParams();
  const qc = useQueryClient();
  const push = useToasts((s) => s.push);
  const { me, ready } = usePrincipal();
  const tab = params.get("tab") === "queue" ? "queue" : "all";
  const [q, setQ] = useState("");
  const [dragOver, setDragOver] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const docs = useQuery({
    queryKey: ["documents"],
    queryFn: () => api.listDocuments(),
    // 処理待ち・処理中の帳票がある間だけ 5 秒ごとに取り直す（無くなれば止まる）。
    // 一括投入した 200 件にジョブ単位のポーリングを張ると API を叩きすぎる。
    refetchInterval: (query) =>
      countRunning(query.state.data?.items ?? []) > 0 ? RUNNING_REFETCH_MS : false,
  });
  const queue = useQuery({ queryKey: ["review-queue"], queryFn: () => api.reviewQueue() });
  const queueCount = queue.data?.items.length ?? 0;

  // 取込時の種別指定。"" = 未指定（＝スキーマなしの自動発見。ADR-0006 の既定）。
  // 候補は GET /doc-types から取る。listSchemas は admin 限定で、アップロードは
  // uploader で通るため、そちらを使うと権限の低い人には選択肢が空になる。
  // 「アップロード後に抽出を開始」もこの一覧の schema_id（種別の最新版）を使う。
  const [docType, setDocType] = useState("");
  const docTypes = useQuery({
    queryKey: ["doc-types"],
    queryFn: () => api.listDocTypes(),
    staleTime: 5 * 60_000,
    retry: false, // 取れなくてもアップロード導線は成立する（未指定で通す）
  });
  // 種別を選んだときだけ効く（既定 on）。未指定＝自動発見は従来どおり帳票ページで
  // 明示的に開始する（ADR-0006「抽出の自動開始はしない」は、種別と一緒に利用者が
  // 選ぶ形で守る。設計 bulk-processing D6）。
  const [autoExtract, setAutoExtract] = useState(true);

  // 逐次アップロードの進行。null = 何もしていない
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null);
  const uploading = progress !== null;

  // 複数ファイルを**逐次**アップロードする（同時に投げると前処理が並列に走って遅く
  // なり、順序も読めなくなる）。docType / autoExtract は開始時点の値で固定する。
  // ボタン経路と D&D 経路の両方から呼ばれるので、引数にせずクロージャで読む
  //（引数にすると片方の配線漏れが「D&D のときだけ無指定」という気付きにくい壊れ方になる）。
  async function uploadAll(list: FileList | null) {
    if (!list || uploading) return;
    const { accepted, rejected } = partitionFiles(Array.from(list));
    if (accepted.length === 0) {
      push({
        kind: "warn",
        message: "取り込める形式のファイルがありません（PDF / PNG / JPEG / TIFF）。",
      });
      return;
    }
    const type = docType || undefined;
    const schemaId = autoExtract && type ? latestSchemaId(docTypes.data?.items, type) : undefined;
    const tally: UploadTally = {
      ok: 0,
      failed: 0,
      rejected: rejected.length,
      extractStarted: 0,
      extractFailed: 0,
    };
    let lastCreated: string | null = null;
    setProgress({ done: 0, total: accepted.length });
    try {
      for (let i = 0; i < accepted.length; i += 1) {
        setProgress({ done: i, total: accepted.length });
        try {
          const created = await api.uploadDocument(accepted[i], type);
          tally.ok += 1;
          lastCreated = created.document_id;
          if (schemaId) {
            try {
              await api.extract(created.document_id, {
                schema_id: schemaId,
                idempotencyKey: newUuid(),
              });
              tally.extractStarted += 1;
            } catch {
              // アップロード自体は成功している。帳票ページから開始できる
              tally.extractFailed += 1;
            }
          }
        } catch {
          tally.failed += 1;
        }
      }
    } finally {
      setProgress(null);
    }
    qc.invalidateQueries({ queryKey: ["documents"] });
    push(summarizeUploads(tally));
    // ちょうど 1 件のときだけ従来どおり帳票ページへ。複数なら次の操作は一覧で行う
    if (accepted.length === 1 && tally.ok === 1 && lastCreated) {
      router.push(`/documents/${lastCreated}`);
    }
  }

  const items = docs.data?.items ?? [];
  const filtered = useMemo(() => {
    const kw = q.trim().toLowerCase();
    if (!kw) return items;
    return items.filter((d) =>
      [d.document_id, d.doc_type, d.external_ref].some((v) => v?.toLowerCase().includes(kw)),
    );
  }, [items, q]);

  // ---- 選択と一括再抽出（設計 bulk-processing §3） ----
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [supersede, setSupersede] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  // 消えた帳票の id は数えない（削除・再取得で一覧から落ちた分）
  const selectedCount = useMemo(
    () => items.filter((d) => selected.has(d.document_id)).length,
    [items, selected],
  );
  const selection = useMemo(() => partitionSelection(items, selected), [items, selected]);
  const allVisibleSelected =
    filtered.length > 0 && filtered.every((d) => selected.has(d.document_id));

  function toggle(id: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }
  function toggleAllVisible() {
    setSelected((prev) => {
      const next = new Set(prev);
      if (allVisibleSelected) filtered.forEach((d) => next.delete(d.document_id));
      else filtered.forEach((d) => next.add(d.document_id));
      return next;
    });
  }

  async function submitBatch() {
    const ids = items.filter((d) => selected.has(d.document_id)).map((d) => d.document_id);
    if (ids.length === 0 || submitting) return;
    setSubmitting(true);
    try {
      const r = await api.extractBatch(
        { document_ids: ids },
        { supersede_review: supersede, idempotencyKey: newUuid() },
      );
      push(summarizeBatch(r));
      setConfirmOpen(false);
      setSelected(new Set());
      qc.invalidateQueries({ queryKey: ["documents"] });
      qc.invalidateQueries({ queryKey: ["review-queue"] });
    } catch (e) {
      push({ kind: "err", message: `再抽出を開始できません（${(e as Error).message}）。` });
    } finally {
      setSubmitting(false);
    }
  }

  const canBulk = ready && hasRole(me.role, "uploader");

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
        {/* 候補が 1 件も無いテナントでは**描画しない**。初回の 1 枚を取り込む人に
            「選ぶべき何かがある」と誤解させないため（ADR-0006 のテンプレートレス優先）。
            既定は必ず「未指定」で、選ばなければリクエストは従来と 1 バイトも変わらない。 */}
        {(docTypes.data?.items.length ?? 0) > 0 && (
          <>
            <label className="upload-doctype">
              <span className="sub">種別</span>
              <select
                value={docType}
                onChange={(e) => setDocType(e.target.value)}
                disabled={uploading}
                aria-label="帳票種別（任意）"
                title="指定すると、この帳票の抽出画面でその定義が既定で選ばれます。未指定でも取り込めます。"
              >
                <option value="">未指定（自動発見）</option>
                {docTypes.data!.items.map((t) => (
                  <option key={t.schema_id} value={t.doc_type}>
                    {t.doc_type}
                  </option>
                ))}
              </select>
            </label>
            {/* 種別を選んだときだけ有効。未指定の帳票は自動発見で、帳票ページから始める */}
            <label
              className="upload-auto sub"
              title={
                docType
                  ? "アップロードした帳票ごとに、その種別の最新の定義で抽出を開始します。"
                  : "種別を選ぶと、アップロード後にその定義で抽出を開始できます。"
              }
            >
              <input
                type="checkbox"
                checked={autoExtract}
                disabled={!docType || uploading}
                onChange={(e) => setAutoExtract(e.target.checked)}
              />
              アップロード後に抽出を開始
            </label>
          </>
        )}
        <input
          ref={fileRef}
          type="file"
          accept={ACCEPT}
          multiple
          hidden
          onChange={(e) => {
            void uploadAll(e.target.files);
            e.target.value = ""; // 同じファイルを続けて選べるように
          }}
        />
        <button className="btn sm grad" onClick={() => fileRef.current?.click()} disabled={uploading}>
          {uploading ? "アップロード中…" : "＋ アップロード"}
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
        {progress && (
          <span className="sub doc-progress" role="status" aria-live="polite">
            {Math.min(progress.done + 1, progress.total)}/{progress.total} 件をアップロード中…
          </span>
        )}
        {tab === "all" && canBulk && selectedCount > 0 && (
          <>
            <span className="spacer" />
            <button
              className="btn sm"
              disabled={submitting || uploading}
              onClick={() => setConfirmOpen(true)}
              title="選択した帳票を、それぞれの種別の最新の定義で取り直します"
            >
              選択した {selectedCount} 件を再抽出
            </button>
            <button className="btn sm ghost" onClick={() => setSelected(new Set())}>
              選択解除
            </button>
          </>
        )}
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
          if (tab === "all") void uploadAll(e.dataTransfer.files);
        }}
      >
        {dragOver && <div className="doc-droplabel">ここにドロップして帳票をアップロード（複数可）</div>}
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
                  {canBulk && (
                    <th className="sel">
                      <input
                        type="checkbox"
                        aria-label="表示中の帳票をすべて選択"
                        title="全選択（表示中の行）"
                        checked={allVisibleSelected}
                        disabled={filtered.length === 0}
                        onChange={toggleAllVisible}
                      />
                    </th>
                  )}
                  <th>帳票</th>
                  <th>種別</th>
                  <th>ページ</th>
                  <th>状態</th>
                  <th>external_ref</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((d) => (
                  <tr
                    key={d.document_id}
                    className={selected.has(d.document_id) ? "selected" : undefined}
                    onClick={() => router.push(`/documents/${d.document_id}`)}
                  >
                    {/* 行クリックは詳細遷移。選択・削除まで伝播すると押した直後に画面が変わる */}
                    {canBulk && (
                      <td className="sel" onClick={(e) => e.stopPropagation()}>
                        <input
                          type="checkbox"
                          aria-label={`${d.document_id} を選択`}
                          checked={selected.has(d.document_id)}
                          onChange={() => toggle(d.document_id)}
                        />
                      </td>
                    )}
                    <td>
                      <span className="docname">{d.document_id}</span>
                    </td>
                    <td>{d.doc_type ?? "—"}</td>
                    <td className="mono">{d.page_count ?? "—"}</td>
                    <td>
                      <StatusChip status={d.status} />
                    </td>
                    <td className="sub">{d.external_ref ?? "—"}</td>
                    <td onClick={(e) => e.stopPropagation()}>
                      <DeleteDocument
                        documentId={d.document_id}
                        label={d.external_ref ?? d.document_id}
                        onDeleted={() => {
                          setSelected((prev) => {
                            if (!prev.has(d.document_id)) return prev;
                            const next = new Set(prev);
                            next.delete(d.document_id);
                            return next;
                          });
                          qc.invalidateQueries({ queryKey: ["documents"] });
                          qc.invalidateQueries({ queryKey: ["review-queue"] });
                          qc.invalidateQueries({ queryKey: ["metrics"] });
                        }}
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {docs.data && filtered.length === 0 && (
              <div className="empty">
                <div className="emoji">🗂️</div>
                <h3>該当するドキュメントがありません</h3>
                <p>帳票をアップロードするか、ここにドラッグ&ドロップしてください（複数可）。</p>
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

      {/* 一括再抽出の確認（設計 bulk-processing §3）。種別の無い帳票はスキップされること、
          確定済みは置き換わらないことを押す前に伝える */}
      {confirmOpen && (
        <div className="tpl-overlay" role="dialog" aria-modal="true" aria-label="選択した帳票を再抽出">
          <div className="tpl-card bulk-dialog">
            <h3>選択した {selectedCount} 件を再抽出します</h3>
            <p>
              種別のある帳票 <b>{selection.withType} 件</b>を、それぞれの種別の最新の定義で取り直します。
              {selection.withoutType > 0 && (
                <>
                  <br />
                  種別の無い帳票 <b>{selection.withoutType} 件</b>は使う定義を決められないためスキップされます
                  （帳票ページから自動発見で抽出できます）。
                </>
              )}
            </p>
            <p className="sub">
              いまの抽出結果と、それに対して入力済みの修正は新しい結果へ引き継がれません。
              確定済み（確定 / 連携済）の帳票はチェックに関係なく置き換えません。
            </p>
            <label className="sub">
              <input
                type="checkbox"
                checked={supersede}
                onChange={(e) => setSupersede(e.target.checked)}
                disabled={submitting}
              />{" "}
              レビュー待ちの結果も置き換える（supersede）
            </label>
            {!supersede && (
              <p className="sub">
                外すと、レビュー待ち（要確認）の帳票は「処理中」としてスキップされます。
              </p>
            )}
            <div className="bulk-actions">
              <button className="btn ghost" onClick={() => setConfirmOpen(false)} disabled={submitting}>
                キャンセル
              </button>
              <button
                className="btn grad"
                onClick={() => void submitBatch()}
                disabled={submitting || selection.withType === 0}
              >
                {submitting ? "投入中…" : "再抽出する"}
              </button>
            </div>
          </div>
        </div>
      )}
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
