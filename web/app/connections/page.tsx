"use client";

// 接続管理（⑤⑥ SaaS連携）。GDrive/S3/DB/Webhook の接続を一覧・作成し、
// gdrive は「今すぐ同期」で監視フォルダを即時に差分検知できる。
// postgres/webhook/s3 は「疎通テスト」で tested にする（ワークフローの有効化に必要。
// フォルダ監視系は「今すぐ同期」の成功が疎通テストを兼ねる）。
// 秘密は config に入れず secret_ref（Secrets Manager / env:）で渡す（§16.5）。

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { AppShell } from "@/components/AppShell";
import { ApiError, api } from "@/lib/api";
import { useToasts } from "@/lib/toast";
import type { ConnectionDto, WorkflowRefDto } from "@/lib/types";

const TYPE_LABEL: Record<string, string> = {
  gdrive: "Google Drive",
  m365: "Microsoft 365",
  box: "Box",
  s3: "S3",
  postgres: "PostgreSQL",
  webhook: "Webhook",
};

// フォルダ監視系（⑤⑥）。作成フォームと「今すぐ同期」を同型で扱う
const FOLDER_TYPES = ["gdrive", "m365", "box"] as const;

const FOLDER_ID_HINT: Record<string, string> = {
  gdrive: "Drive フォルダの ID（URL 末尾の英数字）",
  m365: "<ドライブID>/<フォルダID>（Graph API の drive/item）",
  box: "Box フォルダの ID（数値）",
};

function relTime(iso?: string | null): string {
  if (!iso) return "";
  const ms = Date.now() - new Date(iso).getTime();
  const min = Math.max(0, Math.floor(ms / 60_000));
  if (min < 1) return "1分以内";
  if (min < 60) return `${min}分前`;
  const h = Math.floor(min / 60);
  if (h < 24) return `${h}時間前`;
  return `${Math.floor(h / 24)}日前`;
}

const STATUS_LABEL: Record<string, { cls: string; label: string }> = {
  untested: { cls: "st-uploaded", label: "未テスト" },
  tested: { cls: "st-confirmed", label: "テスト済" },
  active: { cls: "st-confirmed", label: "有効" },
  disabled: { cls: "st-failed", label: "無効" },
};

/** 409(E1005) の details.workflows を「名前（状態）」の並びにする。無ければ空文字 */
function workflowNames(details?: Record<string, unknown>): string {
  const rows = (details?.workflows as WorkflowRefDto[] | undefined) ?? [];
  return rows.map((w) => `「${w.name}」`).join("、");
}

// 無効化・削除の確認とエラー文言。どちらも「何が起きるか」と「次の一手」を必ず入れる。
// 無効化は元に戻せる（再有効化）が、削除は元に戻せない。
function confirmDisable(c: ConnectionDto): boolean {
  return window.confirm(
    `接続「${c.name}」を無効化します。\n` +
      "この接続を使う出力（Webhook / DB 書込み / ファイル）とフォルダ監視・同期は止まります。" +
      "再有効化すれば元に戻ります。\n\nよろしいですか？",
  );
}

function confirmDelete(c: ConnectionDto): boolean {
  // 秘密の扱いは型で違う: Webhook の署名鍵は gateway が作ったので保管先からも消す。
  // それ以外（DB のパスワード等）は利用者が登録した秘密で、参照が消えるだけ
  const secretNote =
    c.type === "webhook"
      ? "署名鍵は保管先（Secrets Manager）からも削除されます。"
      : c.secret_ref
        ? "secret_ref が指す秘密そのものは保管先に残ります。"
        : "";
  return window.confirm(
    `接続「${c.name}」を削除します。\n` +
      "接続の設定（フォルダ・宛先・secret_ref）は消え、元に戻せません。" +
      secretNote +
      "ワークフローから参照されている接続は削除できません（代わりに無効化してください）。\n\n" +
      "よろしいですか？",
  );
}

function AdminDenied() {
  return (
    <AppShell active="connections">
      <div className="topbar">
        <span className="ttl">接続管理</span>
      </div>
      <div className="access-denied">
        <div style={{ fontSize: 30 }}>🔒</div>
        <h3>権限がありません</h3>
        <p>この画面は管理者（admin）のみ利用できます。</p>
      </div>
    </AppShell>
  );
}

function CreateFolderConnectionForm({ onCreated }: { onCreated: () => void }) {
  const push = useToasts((s) => s.push);
  const [open, setOpen] = useState(false);
  const [type, setType] = useState<string>("gdrive");
  const [name, setName] = useState("");
  const [folderId, setFolderId] = useState("");
  const [secretRef, setSecretRef] = useState("");

  const create = useMutation({
    mutationFn: () =>
      api.createConnection({
        type,
        name: name.trim() || TYPE_LABEL[type],
        config: { folder_id: folderId.trim() },
        secret_ref: secretRef.trim() || null,
      }),
    onSuccess: () => {
      push({
        kind: "ok",
        message: `${TYPE_LABEL[type]} 接続を作成しました。ワークフローの「${TYPE_LABEL[type]}」トリガーから選べます。`,
      });
      setOpen(false);
      setName("");
      setFolderId("");
      setSecretRef("");
      onCreated();
    },
    onError: (e) => push({ kind: "warn", message: `作成できません（${(e as Error).message}）。` }),
  });

  if (!open) {
    return (
      <button className="btn sm primary" onClick={() => setOpen(true)}>
        ＋ フォルダ連携を追加（Google Drive / Microsoft 365 / Box）
      </button>
    );
  }

  return (
    <div className="hint-form">
      <div className="hint-row">
        <label>
          サービス
          <select value={type} onChange={(e) => setType(e.target.value)} aria-label="サービス">
            {FOLDER_TYPES.map((t) => (
              <option key={t} value={t}>
                {TYPE_LABEL[t]}
              </option>
            ))}
          </select>
        </label>
        <label>
          接続名
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="例: 経理共有ドライブ"
            aria-label="接続名"
          />
        </label>
        <label>
          監視するフォルダID
          <input
            value={folderId}
            onChange={(e) => setFolderId(e.target.value)}
            placeholder={FOLDER_ID_HINT[type]}
            aria-label="フォルダID"
          />
        </label>
      </div>
      <label className="hint-full">
        認証情報の参照（secret_ref・任意）
        <input
          value={secretRef}
          onChange={(e) => setSecretRef(e.target.value)}
          placeholder="開発モード（モック）では空のまま。本番は ai-ocr/<env>/conn/<テナントID>/<名前> を指定"
          aria-label="secret_ref"
        />
      </label>
      <p className="sub" style={{ margin: 0 }}>
        フォルダに追加されたファイルは自動で取り込まれ、対応するトリガーの
        ワークフローが起動します（環境稼働中のみ・数分間隔）。
        作成後に「今すぐ同期」を1回実行すると疎通確認（テスト済）になり、
        ワークフローの有効化に使えるようになります。
      </p>
      <div className="hint-actions">
        <button
          className="btn sm primary"
          disabled={create.isPending || !folderId.trim()}
          onClick={() => create.mutate()}
        >
          {create.isPending ? "作成中…" : "接続を作成"}
        </button>
        <button className="btn sm ghost" onClick={() => setOpen(false)} disabled={create.isPending}>
          閉じる
        </button>
      </div>
    </div>
  );
}

export default function ConnectionsPage() {
  const qc = useQueryClient();
  const push = useToasts((s) => s.push);
  const { data, error, isPending } = useQuery({
    queryKey: ["connections"],
    queryFn: () => api.listConnections(),
  });

  const sync = useMutation({
    mutationFn: (id: string) => api.syncConnection(id),
    onSuccess: (r) => {
      push(
        r.queued
          ? {
              kind: "ok",
              message:
                "同期を要求しました。反映されない場合は数十秒後にドキュメント一覧を更新してください（環境停止中は取り込まれません）。",
            }
          : { kind: "warn", message: "直前の同期要求を処理中です。少し待ってから再試行してください。" },
      );
    },
    onError: (e) => push({ kind: "warn", message: `同期できません（${(e as Error).message}）。` }),
  });
  // 「今すぐ同期」は行ごとに判定する（単一 mutation の isPending を全行で共有すると
  // 1行の同期中に他の行まで「同期中…」表示・無効化される）
  const syncingId = sync.isPending ? (sync.variables as string) : null;

  // 疎通テスト（postgres/webhook/s3）。結果は行内に残す（トーストだと消えて
  // どの接続が落ちたか分からない）。postgres の失敗は 200 + ok=false、webhook/s3 の
  // 失敗は 422（ApiError）で理由が返るので両方を同じ表示に寄せる
  const [testResults, setTestResults] = useState<Record<string, { ok: boolean; message: string }>>(
    {},
  );
  const test = useMutation({
    mutationFn: (id: string) => api.testConnection(id),
    onSuccess: (r, id) => {
      setTestResults((prev) => ({
        ...prev,
        [id]: r.ok
          ? { ok: true, message: "テスト済" }
          : { ok: false, message: r.message || "疎通テストに失敗しました" },
      }));
      if (r.ok) qc.invalidateQueries({ queryKey: ["connections"] });
    },
    onError: (e, id) => {
      setTestResults((prev) => ({ ...prev, [id]: { ok: false, message: (e as Error).message } }));
    },
  });
  const testingId = test.isPending ? (test.variables as string) : null;

  // 無効化 / 再有効化（C9-D）。有効なワークフローが使っていれば 409 で断られる
  const setStatus = useMutation({
    mutationFn: (v: { c: ConnectionDto; status: "active" | "disabled" }) =>
      api.patchConnectionStatus(v.c.id, v.status),
    onSuccess: (r) => {
      // 疎通テストのある型（postgres / webhook / s3）の再有効化は untested に戻る
      //（テストを踏むまでワークフローに使えない）。サーバの着地点をそのまま伝える
      push({
        kind: r.status === "untested" ? "warn" : "ok",
        message:
          r.status === "disabled"
            ? `「${r.name}」を無効化しました。再有効化すれば元に戻ります。`
            : r.status === "untested"
              ? `「${r.name}」を再有効化しました。この行の「疎通テスト」を通すまでワークフローの有効化には使えません。`
              : `「${r.name}」を再有効化しました。`,
      });
      qc.invalidateQueries({ queryKey: ["connections"] });
    },
    onError: (e, v) => {
      // instanceof は dev のモジュール重複で false になり得るため status/details を直接見る
      const err = e as { status?: number; details?: Record<string, unknown> };
      if (err.status === 409) {
        push({
          kind: "warn",
          message:
            `「${v.c.name}」は有効なワークフロー ${workflowNames(err.details)} が使っているため無効化できません。` +
            "先にワークフローを停止してください。",
        });
        return;
      }
      push({ kind: "err", message: `変更できませんでした（${(e as Error).message}）。` });
    },
  });

  // 削除。参照が残っていれば 409（無効化を案内する）
  const del = useMutation({
    mutationFn: (c: ConnectionDto) => api.deleteConnection(c.id),
    onSuccess: (_r, c) => {
      push({ kind: "ok", message: `「${c.name}」を削除しました。` });
      qc.invalidateQueries({ queryKey: ["connections"] });
    },
    onError: (e, c) => {
      const err = e as { status?: number; details?: Record<string, unknown> };
      if (err.status === 409) {
        const names = workflowNames(err.details);
        const runs = Number(err.details?.run_count ?? 0);
        push({
          kind: "warn",
          message:
            `「${c.name}」はワークフロー${names ? ` ${names}` : ""}` +
            `${runs > 0 ? `${names ? "と" : ""}過去の実行 ${runs} 件` : ""}から参照されているため削除できません。` +
            "使わなくするには「無効化」してください。",
        });
        return;
      }
      push({ kind: "err", message: `削除できませんでした（${(e as Error).message}）。` });
    },
  });
  const busyId = setStatus.isPending
    ? setStatus.variables?.c.id
    : del.isPending
      ? del.variables?.id
      : null;

  if (error instanceof ApiError && error.status === 403) return <AdminDenied />;

  const items = data?.items ?? [];

  return (
    <AppShell active="connections">
      <div className="topbar">
        <span className="ttl">接続管理</span>
        <span className="sub">SaaS・DB・通知先との接続（⑤⑥）</span>
        <span className="spacer" />
      </div>

      <div style={{ padding: "14px 22px 0" }}>
        <CreateFolderConnectionForm
          onCreated={() => qc.invalidateQueries({ queryKey: ["connections"] })}
        />
      </div>

      <div style={{ padding: "14px 22px" }}>
        {isPending && <p>読み込み中…</p>}
        {!isPending && (
          <table className="dtable" style={{ border: "1px solid var(--line)", borderRadius: 12, overflow: "hidden" }}>
            <thead>
              <tr>
                <th>接続</th>
                <th>種別</th>
                <th>設定</th>
                <th>状態</th>
                <th>最終同期</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {items.map((c) => {
                const st = STATUS_LABEL[c.status] ?? { cls: "st-uploaded", label: c.status };
                const summary = (FOLDER_TYPES as readonly string[]).includes(c.type)
                  ? `フォルダ: ${String(c.config.folder_id ?? "—")}`
                  : c.type === "s3"
                    ? `バケット: ${String(c.config.bucket ?? "—")}`
                    : c.type === "postgres"
                      ? `ホスト: ${String(c.config.host ?? "—")}`
                      : String(c.config.url ?? "—");
                return (
                  <tr key={c.id}>
                    <td>
                      <b>{c.name}</b>
                      <div className="sub">{c.id.slice(0, 14)}…</div>
                    </td>
                    <td>{TYPE_LABEL[c.type] ?? c.type}</td>
                    <td className="sub">{summary}</td>
                    <td>
                      <span className={`chip ${st.cls}`}>{st.label}</span>
                    </td>
                    <td className="sub">
                      {c.last_sync_status == null ? (
                        (FOLDER_TYPES as readonly string[]).includes(c.type) ? "—" : ""
                      ) : c.last_sync_status === "ok" ? (
                        <span style={{ color: "var(--green, #2e9e6b)" }}>
                          ✓ {relTime(c.last_synced_at)}
                        </span>
                      ) : (
                        <span
                          style={{ color: "var(--red, #c0392b)" }}
                          title={c.last_sync_error ?? undefined}
                        >
                          ✗ 失敗（{relTime(c.last_synced_at)}）
                        </span>
                      )}
                    </td>
                    <td>
                      <div style={{ display: "flex", alignItems: "center", gap: 6, justifyContent: "flex-end", flexWrap: "wrap" }}>
                        {(FOLDER_TYPES as readonly string[]).includes(c.type) ? (
                          // フォルダ監視系は「今すぐ同期」の成功が疎通テストを兼ねる
                          <button
                            className="btn sm"
                            disabled={syncingId === c.id || c.status === "disabled"}
                            title={c.status === "disabled" ? "無効化された接続は同期できません" : undefined}
                            onClick={() => sync.mutate(c.id)}
                          >
                            {syncingId === c.id ? "同期中…" : "今すぐ同期"}
                          </button>
                        ) : (
                          <>
                            <button
                              className="btn sm"
                              // 無効（disabled）は運用側が止めた印。API も 409 で断る
                              // （テストの成功で tested に戻し配信を再開させない）
                              disabled={testingId === c.id || c.status === "disabled"}
                              onClick={() => test.mutate(c.id)}
                              title={
                                c.status === "disabled"
                                  ? "無効化された接続は疎通テストできません"
                                  : c.type === "webhook"
                                    ? "本配信と同じ署名で {\"event\":\"test\",\"text\":…} を 1 回送ります（2xx で成功。Slack 互換の通知先にはテスト投稿として届きます）"
                                    : c.type === "s3"
                                      ? "バケットの存在と権限（HeadBucket）を確かめます"
                                      : "SELECT 1 で接続を確かめます"
                              }
                            >
                              {testingId === c.id ? "テスト中…" : "疎通テスト"}
                            </button>
                            {testResults[c.id] &&
                              (testResults[c.id].ok ? (
                                <span className="sub" style={{ color: "var(--green, #2e9e6b)" }}>
                                  ✓ {testResults[c.id].message}
                                </span>
                              ) : (
                                <span
                                  className="sub"
                                  style={{ color: "var(--red, #c0392b)", maxWidth: 320 }}
                                  title={testResults[c.id].message}
                                >
                                  ✗ {testResults[c.id].message}
                                </span>
                              ))}
                          </>
                        )}
                        {c.status === "disabled" ? (
                          <button
                            className="btn sm"
                            disabled={busyId === c.id}
                            title="この接続を再び使えるようにする"
                            onClick={() => setStatus.mutate({ c, status: "active" })}
                          >
                            再有効化
                          </button>
                        ) : (
                          <button
                            className="btn sm ghost"
                            disabled={busyId === c.id}
                            title="出力・フォルダ監視を止める（再有効化で戻せます）"
                            onClick={() => {
                              if (confirmDisable(c)) setStatus.mutate({ c, status: "disabled" });
                            }}
                          >
                            無効化
                          </button>
                        )}
                        <button
                          className="btn sm danger"
                          disabled={busyId === c.id}
                          title="この接続を削除する（元に戻せません。参照中は削除できません）"
                          onClick={() => {
                            if (confirmDelete(c)) del.mutate(c);
                          }}
                        >
                          {del.isPending && del.variables?.id === c.id ? "削除中…" : "削除"}
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })}
              {items.length === 0 && (
                <tr>
                  <td colSpan={6} className="sub">
                    接続はまだありません。「＋ フォルダ連携を追加」で Google Drive / Microsoft 365 /
                    Box のフォルダ監視を始められます。
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        )}
      </div>
      <p className="sub" style={{ padding: "0 22px 22px" }}>
        取り込みは環境稼働中のみ・定期ポーリング（既定5分、ローカルは15秒）。停止中に追加された
        ファイルは次回起動時にまとめて取り込まれます。秘密（トークン等）は config に書かず
        secret_ref で渡します（§16.5）。
        「未テスト」の接続はワークフローの有効化に使えません。PostgreSQL / Webhook / S3 は
        「疎通テスト」、フォルダ連携は「今すぐ同期」の成功で「テスト済」になります。
      </p>
    </AppShell>
  );
}
