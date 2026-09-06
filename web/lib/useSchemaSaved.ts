"use client";

// スキーマ保存後のフォロー（設計 §3.1 / §4.4b）。作成（バナーのテンプレート化）と
// 編集（ヘッダの領域・項目を編集）で導線の置き場所は違うが、保存の後にやることは
// 同じなので共有する。
//
// やること 3 つ:
//  1. 適用範囲を正確に伝える（手動抽出と分類推定は最新版・ワークフローは版固定）
//  2. 条件を満たすときだけ「この帳票を再抽出」を出す
//  3. 旧版を参照したままの有効ワークフローを警告する

import { useQueryClient } from "@tanstack/react-query";
import { useCallback } from "react";

import { ApiError, api } from "@/lib/api";
import { useExtractJob } from "@/lib/useExtractJob";
import { useToasts } from "@/lib/toast";
import { newUuid } from "@/lib/uuid";

export interface SchemaSaved {
  docType: string;
  schemaId: string;
  version: number;
  prevSchemaId: string | null;
}

export function useSchemaSaved({
  documentId,
  runStatus,
  readOnly,
  onRefetch,
}: {
  documentId: string;
  runStatus: string;
  readOnly: boolean;
  onRefetch: () => void;
}) {
  const push = useToasts((s) => s.push);
  const qc = useQueryClient();
  const { poll } = useExtractJob();

  const rerun = useCallback(
    (schemaId: string) => {
      // トーストの action は同期 onClick（戻り値を捨てる）なので、await せずに
      // 起動する。await すると未処理 rejection になる。
      void (async () => {
        try {
          const r = await api.extract(documentId, {
            schema_id: schemaId, // ★新版 id を明示送信（未指定はスキーマレス run になり
            //   領域設定が完全に no-op になる）
            supersede_review: true, // ★テンプレート化直後の典型状態は needs_review
            idempotencyKey: newUuid(),
          });
          push({ kind: "info", message: "再抽出を開始しました（数十秒かかります）。" });
          poll(r.job_id, {
            onDone: () => {
              push({ kind: "ok", message: "再抽出が完了しました。" });
              qc.invalidateQueries({ queryKey: ["result", documentId] });
              qc.invalidateQueries({ queryKey: ["documents"] });
              onRefetch();
            },
            onFail: (message) => push({ kind: "warn", message }),
          });
        } catch (e) {
          if (e instanceof ApiError && e.status === 409) {
            push({ kind: "warn", message: "処理中です。完了をお待ちください。" });
          } else {
            push({ kind: "err", message: `再抽出を開始できません（${(e as Error).message}）。` });
          }
        }
      })();
    },
    [documentId, push, poll, qc, onRefetch],
  );

  // 旧版を参照したままの有効ワークフローを探す。put_schema は常に新 uuid の新版
  // INSERT だが、ワークフローの extract ノードは field_schemas.id を固定保持する。
  // ここで見つけられるのは**直前の版**を指しているものだけで、さらに古い版は
  // クライアントから id を辿れない。取りこぼしはサーバ側 lint の L012 が拾う。
  const warnStaleWorkflows = useCallback(
    async (prevSchemaId: string | null) => {
      if (!prevSchemaId) return;
      try {
        const { items } = await api.listWorkflows();
        const actives = items.filter((w) => w.status === "active");
        if (actives.length === 0) return;
        const graphs = await Promise.all(
          actives.map((w) => api.getWorkflow(w.id).catch(() => null)),
        );
        const stale = graphs.filter(
          (g) =>
            g !== null &&
            g.graph_json.nodes.some(
              (n) => n.type === "process.extract" && n.config?.schema_id === prevSchemaId,
            ),
        );
        if (stale.length === 0) return;
        push({
          kind: "warn",
          message:
            `ワークフロー ${stale.length} 件が旧版のスキーマを参照しています` +
            `（${stale.map((g) => g!.name).join("、")}）。` +
            "有効化済みワークフローは版 ID 固定のため、extract ノードのスキーマを" +
            "選び直して再有効化してください。",
        });
      } catch {
        // 警告は補助情報。取得できなくても保存の成否には影響させない
      }
    },
    [push],
  );

  return useCallback(
    (r: SchemaSaved, created: boolean) => {
      qc.invalidateQueries({ queryKey: ["schemas"] });
      // 確定済み（会計連携済みを含む）を無警告で置き換えないため再抽出は出さない。
      // 他者がロック中も同様（サーバも弾くが、押せない方が親切）。
      const canRerun = !readOnly && runStatus !== "confirmed" && runStatus !== "exported";
      push({
        kind: "ok",
        message:
          (created
            ? `スキーマ「${r.docType}」を作成しました（v${r.version}）。`
            : `スキーマ「${r.docType}」を v${r.version} として保存しました。`) +
          "手動抽出・分類推定には最新版が使われます。",
        action: canRerun
          ? { label: "この帳票を再抽出", onClick: () => rerun(r.schemaId) }
          : undefined,
      });
      void warnStaleWorkflows(r.prevSchemaId);
    },
    [push, qc, readOnly, runStatus, rerun, warnStaleWorkflows],
  );
}
