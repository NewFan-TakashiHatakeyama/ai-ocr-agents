"use client";

// SCR-07「実行」タブ（C9-C）。ワークフローの実行履歴・失敗・再実行を UI から見えるようにする。
// バックエンド（GET /workflows/{id}/runs, GET /workflow-runs/{id}, POST /workflow-runs/{id}/retry）
// は実装済みだったが web に配線が無く、失敗しても気付けず、再実行も curl でしかできなかった。
//
// - 一覧: 状態チップ / 開始・終了・所要 / トリガー（発火ノード）/ 失敗ノードとエラー / 帳票へのリンク
// - running / waiting_hitl がある間は 5 秒ごとに自動更新（終端だけなら止める）
// - 行クリックで詳細ドロワー（node_runs を順に、状態・所要時間・エラー）
// - 「再実行」は failed の run だけ（確認 → POST retry）。他は disabled にして理由を title に
//
// 失敗ノードは一覧の DTO に無い（node_runs は詳細だけ）。failed の run に限って詳細を引き、
// 一覧に「失敗箇所」として出す。failed は終端なので finished_at をキーに含めれば再取得は
// 再実行後の 1 回で済む。

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api";
import { useToasts } from "@/lib/toast";
import type { WorkflowNodeRunDto, WorkflowRunItemDto } from "@/lib/types";
import {
  anyRunActive,
  durationMs,
  errorMessage,
  failedNodeOf,
  formatDateTime,
  formatDuration,
  hasRunMovedOn,
  isRunActive,
  nodeStatusView,
  retryBlockedReason,
  runStatusView,
  triggerLabel,
  waitingLabel,
} from "@/lib/workflowRuns";

import { TYPE_LABEL } from "../node-icons";

const POLL_MS = 5_000;
// 動いている run が無いときの緩い更新。トリガー（スケジュール・フォルダ監視）で
// 勝手に始まった run が、タブを開いたまま気付けないのを避ける
const IDLE_POLL_MS = 30_000;
// 再実行を投げてから worker が拾うのを待つ上限。これを過ぎたら 5 秒更新を止める
//（worker 停止中に永久ポーリングしない）。「更新」で手動には引ける
const RETRY_WATCH_MS = 2 * 60_000;
// 一覧で「失敗箇所」を出すために詳細を引く failed run の上限（同時リクエストの抑制）。
// 超えた分は run 単位のエラー文だけ出す
const FAILED_DETAIL_MAX = 20;

type RetryWatch = { finishedAt: string | null; since: number };

function RunChip({ status }: { status: string }) {
  const v = runStatusView(status);
  return <span className={`chip wfrun-${v.tone}`}>{v.label}</span>;
}

function NodeChip({ status, waitingHere }: { status: string; waitingHere: boolean }) {
  const v = nodeStatusView(status, waitingHere);
  return <span className={`chip wfrun-${v.tone}`}>{v.label}</span>;
}

// ノードの表示名。run は開始時点のスナップショットで走るので、現在のグラフに無い
// node_id もあり得る。その場合は node_type の種別名で補う
function nodeName(
  nodeId: string,
  nodeType: string | undefined,
  nodeLabelById: ReadonlyMap<string, string>,
): string {
  return nodeLabelById.get(nodeId) ?? (nodeType ? (TYPE_LABEL[nodeType] ?? nodeType) : nodeId);
}

function RetryButton({
  run,
  pending,
  watching,
  onRetry,
  small,
}: {
  run: WorkflowRunItemDto;
  pending: boolean;
  watching: boolean;
  onRetry: (run: WorkflowRunItemDto) => void;
  small?: boolean;
}) {
  const reason = retryBlockedReason(run.status);
  const blocked = reason ?? (watching ? "再実行を受け付けました。worker が拾うのを待っています" : null);
  // disabled なボタンは title が出ないブラウザがあるので、包む span に理由を持たせる
  return (
    <span className="wfrun-retry" title={blocked ?? undefined}>
      <button
        className={`btn${small ? " sm" : ""}${blocked ? "" : " primary"}`}
        disabled={Boolean(blocked) || pending}
        aria-disabled={Boolean(blocked) || pending}
        title={blocked ? undefined : "失敗した箇所から再実行します（完了済みのノードは走り直しません）"}
        onClick={(e) => {
          e.stopPropagation();
          onRetry(run);
        }}
      >
        {pending ? "送信中…" : "再実行"}
      </button>
    </span>
  );
}

function RunDrawer({
  runId,
  summary,
  watching,
  retryPending,
  nodeLabelById,
  onRetry,
  onClose,
}: {
  runId: string;
  summary: WorkflowRunItemDto | undefined;
  watching: boolean;
  retryPending: boolean;
  nodeLabelById: ReadonlyMap<string, string>;
  onRetry: (run: WorkflowRunItemDto) => void;
  onClose: () => void;
}) {
  const detail = useQuery({
    queryKey: ["workflow-run", runId],
    queryFn: () => api.getWorkflowRun(runId),
    refetchInterval: (q) =>
      watching || (q.state.data && isRunActive(q.state.data.status)) ? POLL_MS : false,
  });

  // Esc で閉じる（オーバーレイのクリックでも閉じる）
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const run = detail.data ?? summary;
  const now = Date.now();
  const waitingNodeId = detail.data?.waiting?.node_id ?? null;
  const waiting = waitingLabel(detail.data?.waiting);
  const runErr = errorMessage(run?.error);
  const nodeRuns: WorkflowNodeRunDto[] = detail.data?.node_runs ?? [];

  return (
    <div className="wfrun-overlay" onClick={onClose}>
      <aside
        className="wfrun-drawer"
        role="dialog"
        aria-label="実行の詳細"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="wfrun-drawer-head">
          <div className="wfrun-drawer-title">
            <span className="wfrun-id">{runId}</span>
            {run && <RunChip status={run.status} />}
          </div>
          <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
            {run && (
              <RetryButton
                run={run}
                pending={retryPending}
                watching={watching}
                onRetry={onRetry}
                small
              />
            )}
            <button className="btn sm ghost" aria-label="閉じる" onClick={onClose}>
              ×
            </button>
          </div>
        </div>

        {detail.isError && (
          <div className="wfrun-err">
            詳細を取得できませんでした（{(detail.error as Error).message}）。
            <button className="btn sm ghost" onClick={() => detail.refetch()}>
              再試行
            </button>
          </div>
        )}

        {run && (
          <dl className="wfrun-meta">
            <dt>開始</dt>
            <dd>{formatDateTime(run.started_at)}</dd>
            <dt>終了</dt>
            <dd>{formatDateTime(run.finished_at)}</dd>
            <dt>所要</dt>
            <dd>
              {formatDuration(
                durationMs(run.started_at, run.finished_at, isRunActive(run.status) ? now : undefined),
              )}
            </dd>
            <dt>トリガー</dt>
            <dd>
              {triggerLabel(run.trigger_type)}
              {run.trigger_node_id && (
                <span className="wf-nodetag">
                  {nodeName(run.trigger_node_id, undefined, nodeLabelById)}
                </span>
              )}
            </dd>
            <dt>版</dt>
            <dd>v{run.workflow_version}</dd>
            <dt>帳票</dt>
            <dd>
              {run.document_id ? (
                <Link href={`/documents/${run.document_id}`} className="wfrun-link">
                  {run.document_id}
                </Link>
              ) : (
                "—"
              )}
            </dd>
          </dl>
        )}

        {waiting && (
          <div className="wfrun-waiting">
            {waiting}
            {waitingNodeId && (
              <span className="wf-nodetag">{nodeName(waitingNodeId, undefined, nodeLabelById)}</span>
            )}
          </div>
        )}

        {runErr && (
          <div className="wfrun-err">
            <div className="wfrun-err-label">エラー</div>
            <pre className="wfrun-pre">{runErr}</pre>
          </div>
        )}

        <div className="wf-pane-title" style={{ marginTop: 14 }}>
          ノードの実行
        </div>
        {detail.isLoading && !detail.data && <div className="sub">読み込み中…</div>}
        {detail.data && nodeRuns.length === 0 && (
          <div className="sub">まだノードは実行されていません（worker が拾うと表示されます）。</div>
        )}
        <ol className="wfrun-nodes">
          {nodeRuns.map((n, i) => {
            const waitingHere = waitingNodeId === n.node_id && isRunActive(run?.status ?? "");
            const err = errorMessage(n.error);
            const ms = durationMs(
              n.started_at,
              n.finished_at,
              n.status === "running" ? now : undefined,
            );
            return (
              <li key={`${n.node_id}:${i}`} className={`wfrun-node ${nodeStatusView(n.status, waitingHere).tone}`}>
                <div className="wfrun-node-head">
                  <span className="wfrun-node-idx">{i + 1}</span>
                  <span className="wfrun-node-name">
                    {nodeName(n.node_id, n.node_type, nodeLabelById)}
                    <span className="wf-nodetag">{n.node_id}</span>
                  </span>
                  <NodeChip status={n.status} waitingHere={waitingHere} />
                </div>
                <div className="wfrun-node-sub">
                  <span>{formatDateTime(n.started_at)}</span>
                  <span>所要 {formatDuration(ms)}</span>
                  {n.attempt > 1 && <span>{n.attempt} 回目</span>}
                </div>
                {err && <pre className="wfrun-pre">{err}</pre>}
              </li>
            );
          })}
        </ol>
      </aside>
    </div>
  );
}

export function RunsPanel({
  workflowId,
  nodeLabelById,
}: {
  workflowId: string;
  nodeLabelById: ReadonlyMap<string, string>;
}) {
  const qc = useQueryClient();
  const push = useToasts((s) => s.push);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  // 再実行を投げた run。worker が拾うまで status は failed のままなので、
  // これがある間も一覧を 5 秒ごとに引く（動き出したら外す。RETRY_WATCH_MS で諦める）
  const [retryWatch, setRetryWatch] = useState<Record<string, RetryWatch>>({});
  const watchingAny = Object.keys(retryWatch).length > 0;

  const runs = useQuery({
    queryKey: ["workflow-runs", workflowId],
    queryFn: () => api.listWorkflowRuns(workflowId),
    refetchInterval: (q) =>
      watchingAny || anyRunActive(q.state.data?.items ?? []) ? POLL_MS : IDLE_POLL_MS,
  });
  const items = useMemo(() => runs.data?.items ?? [], [runs.data]);

  // 再実行の監視を解く: 動き出した / 再び終わった / 見当たらない / 時間切れ。
  // 内容が同じ応答は structural sharing で items が変わらないため、時間切れの判定は
  // dataUpdatedAt（取得のたびに進む）で回す
  useEffect(() => {
    if (!watchingAny || !runs.data) return;
    const now = Date.now();
    const next: Record<string, RetryWatch> = {};
    let changed = false;
    for (const [id, w] of Object.entries(retryWatch)) {
      const run = items.find((r) => r.id === id);
      if (hasRunMovedOn(w, run) || now - w.since > RETRY_WATCH_MS) {
        changed = true;
        qc.invalidateQueries({ queryKey: ["workflow-run", id] });
      } else {
        next[id] = w;
      }
    }
    if (changed) setRetryWatch(next);
  }, [items, runs.data, runs.dataUpdatedAt, watchingAny, retryWatch, qc]);

  // failed の run だけ詳細を引いて「失敗箇所」を一覧に出す
  const failedRuns = useMemo(
    () => items.filter((r) => r.status === "failed").slice(0, FAILED_DETAIL_MAX),
    [items],
  );
  const failedDetails = useQueries({
    queries: failedRuns.map((r) => ({
      queryKey: ["workflow-run", r.id, r.finished_at ?? ""],
      queryFn: () => api.getWorkflowRun(r.id),
      staleTime: 5 * 60_000,
    })),
  });
  // 取れた run: 失敗ノード（ノード外で落ちたら null）。引いている最中の run は "loading"
  const failedNodeById = new Map<string, WorkflowNodeRunDto | null | "loading">();
  failedRuns.forEach((r, i) => {
    const d = failedDetails[i]?.data;
    failedNodeById.set(r.id, d ? failedNodeOf(d.node_runs) : "loading");
  });

  const retry = useMutation({
    mutationFn: (runId: string) => api.retryWorkflowRun(runId),
    onSuccess: (_r, runId) => {
      const run = items.find((r) => r.id === runId);
      setRetryWatch((w) => ({
        ...w,
        [runId]: { finishedAt: run?.finished_at ?? null, since: Date.now() },
      }));
      qc.invalidateQueries({ queryKey: ["workflow-runs", workflowId] });
      qc.invalidateQueries({ queryKey: ["workflow-run", runId] });
      push({ kind: "ok", message: "再実行を受け付けました。失敗した箇所から続きが走ります。" });
    },
    onError: (e) =>
      push({ kind: "warn", message: `再実行できません（${(e as Error).message}）。` }),
  });

  const confirmRetry = (run: WorkflowRunItemDto) => {
    if (retryBlockedReason(run.status) || retryWatch[run.id]) return;
    const ok = window.confirm(
      `この実行（${run.id}）を失敗した箇所から再実行します。\n` +
        "完了済みのノードは走り直しません。\n\n" +
        "よろしいですか？",
    );
    if (ok) retry.mutate(run.id);
  };

  const polling = watchingAny || anyRunActive(items);
  const now = Date.now();
  const selected = selectedId ? items.find((r) => r.id === selectedId) : undefined;
  const closeDrawer = useCallback(() => setSelectedId(null), []);

  return (
    <div className="wfrun-panel">
      <div className="wfrun-toolbar">
        <div>
          <div className="wf-pane-title" style={{ margin: 0 }}>
            実行履歴
            <span className="sub" style={{ marginLeft: 8 }}>
              新しい順・最新 50 件
            </span>
          </div>
          <div className="sub" style={{ marginTop: 2 }}>
            {polling
              ? "実行中のものがあるため 5 秒ごとに更新しています"
              : "実行中のものはありません（30 秒ごとに確認します）"}
          </div>
        </div>
        {/* 5 秒更新中に disabled が点滅しないよう、取得中でも押せるままにする（重複は react-query が畳む） */}
        <button className="btn sm" onClick={() => runs.refetch()} title="いま取り直す">
          更新
        </button>
      </div>

      {runs.isError && (
        <div className="wfrun-err" style={{ margin: "0 22px" }}>
          実行履歴を取得できませんでした（{(runs.error as Error).message}）。
          <button className="btn sm ghost" onClick={() => runs.refetch()}>
            再試行
          </button>
        </div>
      )}

      {runs.isLoading && <div className="sub" style={{ padding: "12px 22px" }}>読み込み中…</div>}

      {runs.data && items.length === 0 && (
        <div className="empty">
          まだ実行がありません。帳票の画面で「ワークフローで処理」を押すか、トリガーの発火を待ってください。
        </div>
      )}

      {items.length > 0 && (
        <table className="dtable wfrun-table">
          <thead>
            <tr>
              <th>状態</th>
              <th>開始</th>
              <th>終了 / 所要</th>
              <th>トリガー</th>
              <th>失敗箇所</th>
              <th>帳票</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {items.map((r) => {
              const active = isRunActive(r.status);
              const ms = durationMs(r.started_at, r.finished_at, active ? now : undefined);
              // undefined = 引いていない（failed でない / 上限超え）, "loading" = 取得中,
              // null = ノード外で失敗, それ以外 = 失敗ノード
              const failedNode = r.status === "failed" ? failedNodeById.get(r.id) : undefined;
              const failedNodeRun =
                failedNode && failedNode !== "loading" ? failedNode : null;
              const err = errorMessage(failedNodeRun?.error) ?? errorMessage(r.error);
              return (
                <tr
                  key={r.id}
                  className={selectedId === r.id ? "on" : undefined}
                  onClick={() => setSelectedId(r.id)}
                >
                  <td>
                    <RunChip status={r.status} />
                    <div className="sub wfrun-id" style={{ marginTop: 3 }}>
                      {r.id}
                    </div>
                  </td>
                  <td className="wfrun-nowrap">{formatDateTime(r.started_at)}</td>
                  <td className="wfrun-nowrap">
                    {formatDateTime(r.finished_at)}
                    <div className="sub">
                      {active ? "経過 " : "所要 "}
                      {formatDuration(ms)}
                    </div>
                  </td>
                  <td className="wfrun-nowrap">
                    {triggerLabel(r.trigger_type)}
                    {r.trigger_node_id && (
                      <span className="wf-nodetag">
                        {nodeName(r.trigger_node_id, undefined, nodeLabelById)}
                      </span>
                    )}
                  </td>
                  <td className="wfrun-failcell">
                    {r.status === "failed" ? (
                      <>
                        {failedNodeRun ? (
                          <span className="wfrun-failnode">
                            {nodeName(failedNodeRun.node_id, failedNodeRun.node_type, nodeLabelById)}
                            <span className="wf-nodetag">{failedNodeRun.node_id}</span>
                          </span>
                        ) : failedNode === null ? (
                          <span className="sub">（ノード外で失敗）</span>
                        ) : failedNode === "loading" ? (
                          <span className="sub">…</span>
                        ) : null}
                        {err && (
                          <div className="wfrun-errline" title={err}>
                            {err}
                          </div>
                        )}
                      </>
                    ) : (
                      <span className="sub">—</span>
                    )}
                  </td>
                  <td className="wfrun-nowrap">
                    {r.document_id ? (
                      <Link
                        href={`/documents/${r.document_id}`}
                        className="wfrun-link"
                        onClick={(e) => e.stopPropagation()}
                        title="この帳票を開く"
                      >
                        {r.document_id}
                      </Link>
                    ) : (
                      <span className="sub">—</span>
                    )}
                  </td>
                  <td className="wfrun-nowrap" style={{ textAlign: "right" }}>
                    <RetryButton
                      run={r}
                      pending={retry.isPending && retry.variables === r.id}
                      watching={Boolean(retryWatch[r.id])}
                      onRetry={confirmRetry}
                      small
                    />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}

      {selectedId && (
        <RunDrawer
          runId={selectedId}
          summary={selected}
          watching={Boolean(retryWatch[selectedId])}
          retryPending={retry.isPending && retry.variables === selectedId}
          nodeLabelById={nodeLabelById}
          onRetry={confirmRetry}
          onClose={closeDrawer}
        />
      )}
    </div>
  );
}
