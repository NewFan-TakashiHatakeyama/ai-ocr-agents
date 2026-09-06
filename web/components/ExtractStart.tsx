"use client";

// まだ抽出していない帳票（run 無し）で「抽出を開始」する導線。
// アップロード直後の SCR-03 が「取得失敗」で行き止まりになるのを解消する（①-d）。
// スキーマを選んで抽出 → ジョブ完了までポーリング → 完了で結果画面へ切り替え。

import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "@/lib/api";
import { useExtractJob } from "@/lib/useExtractJob";
import { newUuid } from "@/lib/uuid";
import { useToasts } from "@/lib/toast";

type Phase = "idle" | "running" | "error";

export function ExtractStart({ documentId, onDone }: { documentId: string; onDone: () => void }) {
  const push = useToasts((s) => s.push);
  const [schemaId, setSchemaId] = useState<string>("");
  const [touched, setTouched] = useState(false);
  const [phase, setPhase] = useState<Phase>("idle");
  const { poll } = useExtractJob();

  const schemas = useQuery({ queryKey: ["schemas"], queryFn: () => api.listSchemas() });
  const items = schemas.data?.items ?? [];

  // 帳票種別を自動推定してスキーマを既定選択（⑦）。ユーザーが触るまでは推定を尊重する。
  const classify = useQuery({
    queryKey: ["classify", documentId],
    queryFn: () => api.classifyDocument(documentId),
    staleTime: 5 * 60_000,
    retry: false,
  });
  const suggested = classify.data?.suggested_schema_id || "";

  // 優先度: ユーザー選択 > 自動推定 > スキーマなし（自動発見）。
  // 以前は先頭スキーマへ強制割当していたが、テンプレートが未整備の段階で
  // 無関係なスキーマに当てはめるのは誤抽出のもと（設計見直し: まず値を見てから
  // テンプレート化する。ADR-0006）。推定が付かなければスキーマなしで抽出する。
  const effectiveSchema = touched ? schemaId : (schemaId || suggested || "");

  const start = useMutation({
    mutationFn: () =>
      api.extract(documentId, {
        schema_id: effectiveSchema || undefined,
        idempotencyKey: newUuid(),
      }),
    onSuccess: (r) => {
      setPhase("running");
      poll(r.job_id, {
        onDone: () => {
          push({ kind: "ok", message: "抽出が完了しました。" });
          onDone();
        },
        onFail: (message) => {
          setPhase("error");
          push({ kind: "warn", message });
        },
      });
    },
    onError: (e) =>
      push({ kind: "warn", message: `抽出を開始できません（${(e as Error).message}）。` }),
  });

  const running = start.isPending || phase === "running";

  return (
    <div className="extract-start">
      <div className="emoji">📄</div>
      <h2>まだ抽出していません</h2>
      <p>
        この帳票の AI-OCR 抽出を開始します。スキーマ（抽出する項目の定義）が未登録でも、
        まず項目を自動発見して値を抽出できます。結果を見てからテンプレート化できます。
      </p>

      {suggested && !touched && classify.data?.doc_type && (
        <div className="extract-suggest" role="status">
          🔎 推定: <b>{classify.data.doc_type}</b>
          {classify.data.method !== "declared" && (
            <span className="sub"> · 信頼度 {Math.round((classify.data.confidence ?? 0) * 100)}%</span>
          )}
          <span className="sub">（{classify.data.reason}）</span>
          <span className="sub">— 違う場合は下で選び直せます</span>
        </div>
      )}

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
          {/* スキーマ指定は任意。未登録の帳票種でも行き止まりにしない（ADR-0006） */}
          <option value="">スキーマなし — 項目を自動発見</option>
          {/* GET /schemas は admin 限定のため、reviewer 等では一覧が空のまま
              分類推定だけが付くことがある。選択肢に無い値を select に入れると
              「自動発見と表示しながら推定スキーマで抽出する」嘘になるので、
              推定分を明示の選択肢として足す */}
          {suggested && !items.some((s) => s.id === suggested) && (
            <option value={suggested}>推定: {classify.data?.doc_type ?? suggested}</option>
          )}
          {items.map((s) => (
            <option key={s.id} value={s.id}>
              {s.doc_type}（v{s.version}）
            </option>
          ))}
        </select>
      </label>

      {!effectiveSchema && (
        <p className="sub" style={{ margin: "4px 0 0" }}>
          帳票から見出しと値の組を AI が自動で見つけます。抽出後、結果画面から
          スキーマとして保存（テンプレート化）できます。
        </p>
      )}

      <button className="btn grad" disabled={running} onClick={() => start.mutate()}>
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
