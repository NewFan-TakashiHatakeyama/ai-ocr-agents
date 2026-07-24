"use client";

// まだ抽出していない帳票（run 無し）で「抽出を開始」する導線。
// アップロード直後の SCR-03 が「取得失敗」で行き止まりになるのを解消する（①-d）。
// スキーマを選んで抽出 → ジョブ完了までポーリング → 完了で結果画面へ切り替え。

import { useMutation, useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { api } from "@/lib/api";
import { useToasts } from "@/lib/toast";

type Phase = "idle" | "running" | "error";

export function ExtractStart({ documentId, onDone }: { documentId: string; onDone: () => void }) {
  const push = useToasts((s) => s.push);
  const [schemaId, setSchemaId] = useState<string>("");
  const [touched, setTouched] = useState(false);
  const [phase, setPhase] = useState<Phase>("idle");
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const schemas = useQuery({ queryKey: ["schemas"], queryFn: () => api.listSchemas() });
  const items = schemas.data?.items ?? [];
  // 未選択なら先頭スキーマを既定に（項目を出すにはスキーマ指定が要る）
  const effectiveSchema = touched ? schemaId : (schemaId || items[0]?.id || "");

  // ジョブ完了まで命令的にポーリング（react-query の refetchInterval より確実）
  function pollJob(jobId: string) {
    const tick = async () => {
      if (!alive.current) return;
      try {
        const j = await api.getJob(jobId);
        if (!alive.current) return;
        if (j.status === "succeeded") {
          push({ kind: "ok", message: "抽出が完了しました。" });
          onDone();
          return;
        }
        if (j.status === "failed") {
          setPhase("error");
          push({ kind: "warn", message: "抽出に失敗しました。時間をおいて再試行してください。" });
          return;
        }
      } catch {
        /* 一時エラーは無視して継続 */
      }
      if (alive.current) setTimeout(tick, 1500);
    };
    tick();
  }

  const start = useMutation({
    mutationFn: () => api.extract(documentId, { schema_id: effectiveSchema || undefined }),
    onSuccess: (r) => {
      setPhase("running");
      pollJob(r.job_id);
    },
    onError: (e) =>
      push({ kind: "warn", message: `抽出を開始できません（${(e as Error).message}）。` }),
  });

  const running = start.isPending || phase === "running";

  return (
    <div className="extract-start">
      <div className="emoji">📄</div>
      <h2>まだ抽出していません</h2>
      <p>この帳票の AI-OCR 抽出を開始します。使用するスキーマ（抽出する項目の定義）を選んでください。</p>

      <label className="extract-field">
        <span>スキーマ</span>
        <select
          value={effectiveSchema}
          disabled={running || schemas.isLoading}
          onChange={(e) => {
            setTouched(true);
            setSchemaId(e.target.value);
          }}
        >
          {items.length === 0 && <option value="">（スキーマ未登録）</option>}
          {items.map((s) => (
            <option key={s.id} value={s.id}>
              {s.doc_type}（v{s.version}）
            </option>
          ))}
        </select>
      </label>

      <button className="btn grad" disabled={running || !effectiveSchema} onClick={() => start.mutate()}>
        {running ? "抽出中…（数十秒かかります）" : "抽出を開始"}
      </button>
      {running && <p className="extract-hint">完了すると自動でこの画面に結果が表示されます。</p>}
      {phase === "error" && (
        <button className="btn" onClick={() => start.mutate()}>
          もう一度試す
        </button>
      )}
    </div>
  );
}
