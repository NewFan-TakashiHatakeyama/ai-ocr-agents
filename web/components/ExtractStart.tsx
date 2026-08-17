"use client";

// まだ抽出していない帳票（run 無し）で「抽出を開始」する導線。
// アップロード直後の SCR-03 が「取得失敗」で行き止まりになるのを解消する（①-d）。
// スキーマを選んで抽出 → ジョブ完了までポーリング → 完了で結果画面へ切り替え。

import { useMutation, useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { ApiError, api } from "@/lib/api";
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

  // ジョブ完了まで命令的にポーリング（react-query の refetchInterval より確実）。
  // 締切・dead 終端・恒久エラーで必ず止める（停滞ジョブでの無限ループを防ぐ）。
  function pollJob(jobId: string) {
    const startedAt = Date.now();
    const DEADLINE_MS = 3 * 60_000; // 3分で打ち切り（キュー滞留/ワーカー停止対策）
    const fail = (message: string) => {
      if (!alive.current) return;
      setPhase("error");
      push({ kind: "warn", message });
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
          push({ kind: "ok", message: "抽出が完了しました。" });
          onDone();
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
      if (alive.current) setTimeout(tick, 1500);
    };
    tick();
  }

  const start = useMutation({
    mutationFn: () =>
      api.extract(documentId, {
        schema_id: effectiveSchema || undefined,
        idempotencyKey: crypto.randomUUID(),
      }),
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
