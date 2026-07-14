"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { use, useCallback, useEffect, useMemo, useState } from "react";

import { DocViewer } from "@/components/DocViewer";
import { FieldPanel } from "@/components/FieldPanel";
import { StatusChip } from "@/components/StatusChip";
import { ApiError, api } from "@/lib/api";
import { sortFields } from "@/lib/fields";
import { useReviewStore } from "@/lib/store";
import { useToasts } from "@/lib/toast";
import { fmtRemaining, useDocumentLock } from "@/lib/useDocumentLock";

// SCR-03 検証画面（HITL, §8.2/§8.3）。本プロダクトの中核。全操作キーボード完結（§8.4）。
export default function ReviewPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["result", id],
    queryFn: () => api.getResult(id),
  });
  const { selectedField, selectedCell, edits, select, setEdit, clearEdits } = useReviewStore();
  const push = useToasts((s) => s.push);
  const [busy, setBusy] = useState(false);
  const [page, setPage] = useState(1);
  // §8.2 ソフトロック: 他者が確認中なら readOnly（編集・確定を抑止）
  const { readOnly, remaining, holder } = useDocumentLock(id);

  const pending = useMemo(
    () => (data ? sortFields(data.fields).filter((f) => f.review_status === "pending") : []),
    [data],
  );
  const pages = useMemo(() => {
    const s = new Set<number>();
    data?.fields.forEach((f) => f.bbox && s.add(f.page ?? 1));
    return s.size ? [...s].sort((a, b) => a - b) : [1];
  }, [data]);

  useEffect(() => {
    // 選択フィールド/セルのページを表示
    const f = data?.fields.find((x) => x.name === selectedField);
    if (f?.page) setPage(f.page);
    else if (selectedCell) setPage(selectedCell.page);
  }, [selectedField, selectedCell, data]);

  const submit = useCallback(
    async (force = false) => {
      if (!data) return;
      const pendingCount = Number(data.review_summary?.pending ?? 0);
      if (pendingCount > 0 && !force) {
        if (!window.confirm(`未確認が ${pendingCount} 件あります。確定してよいですか？`)) return;
      }
      setBusy(true);
      try {
        const items = Object.entries(edits).map(([field_name, corrected_value]) => ({
          field_name,
          original_value: data.fields.find((f) => f.name === field_name)?.value_normalized ?? null,
          corrected_value,
        }));
        if (items.length > 0) {
          await api.postCorrections(data.document_id, data.run_id, data.result_version, items);
        }
        await api.confirm(data.document_id, data.run_id);
        clearEdits();
        push({ kind: "ok", message: "確定しました。会計システムへ連携します（完了は通知されます）。" });
        refetch();
      } catch (e) {
        if (e instanceof ApiError && e.status === 409) {
          push({
            kind: "warn",
            message: "他のメンバーが先に更新しました。最新を読み込みます。",
            action: { label: "最新を表示", onClick: () => refetch() },
          });
        } else {
          push({ kind: "err", message: `確定に失敗しました（${(e as Error).message}）。時間をおいて再試行してください。` });
        }
      } finally {
        setBusy(false);
      }
    },
    [data, edits, clearEdits, push, refetch],
  );

  // §8.4 キーボード完結
  useEffect(() => {
    function onKey(ev: KeyboardEvent) {
      const typing = ev.target instanceof HTMLInputElement || ev.target instanceof HTMLTextAreaElement;
      if ((ev.metaKey || ev.ctrlKey) && ev.key === "Enter") {
        ev.preventDefault();
        if (!readOnly) void submit();
        return;
      }
      if (typing) {
        if (ev.key === "Escape") (ev.target as HTMLInputElement).blur();
        return;
      }
      if (pending.length === 0) return;
      const idx = pending.findIndex((f) => f.name === selectedField);
      if (ev.key === "n" || ev.key === "Enter") {
        ev.preventDefault();
        select(pending[(idx + 1 + pending.length) % pending.length].name);
      } else if (ev.key === "p") {
        ev.preventDefault();
        select(pending[(idx - 1 + pending.length) % pending.length].name);
      } else if (ev.key === "e" && idx < 0 && pending[0]) {
        select(pending[0].name);
      } else if ((ev.key === "1" || ev.key === "2") && idx >= 0 && !readOnly) {
        // 候補採択: 1=OCR原値 / 2=LLM補正案（§8.3 CharDiffPopover）
        const f = pending[idx];
        const corr = (f.correction ?? {}) as { to?: string };
        const val = ev.key === "1" ? f.value_raw ?? "" : corr.to ?? f.value_raw ?? "";
        setEdit(f.name, val);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [pending, selectedField, select, setEdit, submit, readOnly]);

  // §8.3 No.4 autosave: 編集を 500ms デバウンスで POST /corrections（version 付き楽観ロック）
  const [saveState, setSaveState] = useState<"idle" | "saving" | "saved">("idle");
  useEffect(() => {
    if (!data || readOnly || Object.keys(edits).length === 0) return;
    const handle = setTimeout(async () => {
      const items = Object.entries(edits).map(([field_name, corrected_value]) => ({
        field_name,
        original_value: data.fields.find((f) => f.name === field_name)?.value_normalized ?? null,
        corrected_value,
      }));
      setSaveState("saving");
      try {
        await api.postCorrections(data.document_id, data.run_id, data.result_version, items);
        setSaveState("saved");
      } catch (e) {
        setSaveState("idle");
        if (e instanceof ApiError && e.status === 409) {
          push({
            kind: "warn",
            message: "他のメンバーが先に更新しました。最新を読み込みます。",
            action: { label: "最新を表示", onClick: () => refetch() },
          });
        }
        // その他のエラーは静かに（確定時に再送される）
      }
    }, 500);
    return () => clearTimeout(handle);
  }, [edits, data, push, refetch, readOnly]);

  if (isLoading) return <div className="page">読み込み中…</div>;
  if (error || !data) return <div className="page">結果の取得に失敗しました。</div>;

  const auto = Number(data.review_summary?.auto ?? data.fields.filter((f) => f.review_status !== "pending").length);
  const pend = Number(data.review_summary?.pending ?? pending.length);
  const total = auto + pend || 1;

  return (
    <div className="main">
      <div className="rv-head">
        <Link href="/documents?tab=queue" className="btn sm ghost">
          ← 一覧
        </Link>
        <div>
          <b className="rv-title">{data.document_id}</b>
          <span className="sub"> · run {data.run_id.slice(0, 12)}</span>
        </div>
        <StatusChip status={data.status} />
        {saveState !== "idle" && (
          <span className="sub" aria-live="polite">
            {saveState === "saving" ? "保存中…" : "✓ 保存済み"}
          </span>
        )}
        <span className="spacer" />
        <div className="rv-prog">
          <span className="seg" aria-hidden>
            <i style={{ width: `${(auto / total) * 100}%`, background: "var(--green)" }} />
            <i style={{ width: `${(pend / total) * 100}%`, background: "var(--amber)" }} />
          </span>
          確定 {auto} ·{" "}
          <b style={{ color: pend ? "var(--amber-ink)" : "var(--green)" }}>要確認 {pend}</b>
        </div>
        <button
          className="btn primary"
          disabled={busy || readOnly || data.status === "confirmed"}
          onClick={() => submit()}
        >
          {busy ? "処理中…" : pend > 0 ? `確定（要確認 ${pend}件）` : "確定して連携へ"}
        </button>
      </div>

      {readOnly && (
        <div className="rv-lock" role="status" aria-live="polite">
          🔒 {holder ?? "他のメンバー"} が確認中です（残り {fmtRemaining(remaining)}）。
          このドキュメントは読み取り専用です。
        </div>
      )}

      <div className="rv-body">
        <div className="viewer">
          <div className="v-tools">
            {pages.map((p) => (
              <button key={p} className={`pagetab${p === page ? " on" : ""}`} onClick={() => setPage(p)}>
                p.{p}
              </button>
            ))}
            <span className="spacer" />
            <span className="sub">前処理後PNG＝座標系の正（DD-01）</span>
          </div>
          <DocViewer documentId={id} fields={data.fields} pageNo={page} />
        </div>
        <FieldPanel fields={data.fields} tables={data.tables} readOnly={readOnly} />
      </div>

      <div className="rv-foot">
        <span>
          <span className="kbd">n</span>/<span className="kbd">p</span> 次/前の要確認
        </span>
        <span>
          <span className="kbd">Enter</span> 次へ
        </span>
        <span>
          <span className="kbd">e</span> 編集
        </span>
        <span>
          <span className="kbd">⌘</span>＋<span className="kbd">Enter</span> 確定
        </span>
        <span className="spacer" />
        <span style={{ display: "flex", gap: 6, alignItems: "center" }}>
          由来:
          <span className="src ocr">OCR</span>
          <span className="src llm">LLM</span>
          <span className="src rule">ルール</span>
          <span className="src human">人手</span>
          <span className="src vl">VL</span>
        </span>
      </div>
    </div>
  );
}
