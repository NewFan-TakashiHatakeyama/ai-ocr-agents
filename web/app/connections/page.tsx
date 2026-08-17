"use client";

// 接続管理（⑤⑥ SaaS連携）。GDrive/S3/DB/Webhook の接続を一覧・作成し、
// gdrive は「今すぐ同期」で監視フォルダを即時に差分検知できる。
// 秘密は config に入れず secret_ref（Secrets Manager / env:）で渡す（§16.5）。

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { AppShell } from "@/components/AppShell";
import { ApiError, api } from "@/lib/api";
import { useToasts } from "@/lib/toast";

const TYPE_LABEL: Record<string, string> = {
  gdrive: "Google Drive",
  s3: "S3",
  postgres: "PostgreSQL",
  webhook: "Webhook",
};

const STATUS_LABEL: Record<string, { cls: string; label: string }> = {
  untested: { cls: "st-uploaded", label: "未テスト" },
  tested: { cls: "st-confirmed", label: "テスト済" },
  active: { cls: "st-confirmed", label: "有効" },
  disabled: { cls: "st-failed", label: "無効" },
};

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

function CreateGDriveForm({ onCreated }: { onCreated: () => void }) {
  const push = useToasts((s) => s.push);
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [folderId, setFolderId] = useState("");
  const [secretRef, setSecretRef] = useState("");

  const create = useMutation({
    mutationFn: () =>
      api.createConnection({
        type: "gdrive",
        name: name.trim() || "Google Drive",
        config: { folder_id: folderId.trim() },
        secret_ref: secretRef.trim() || null,
      }),
    onSuccess: () => {
      push({
        kind: "ok",
        message:
          "Google Drive 接続を作成しました。ワークフローの「Google Drive」トリガーから選べます。",
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
        ＋ Google Drive を接続
      </button>
    );
  }

  return (
    <div className="hint-form">
      <div className="hint-row">
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
            placeholder="Drive フォルダの ID（URL 末尾の英数字）"
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
        フォルダに追加されたファイルは自動で取り込まれ、Google Drive
        トリガーのワークフローが起動します（環境稼働中のみ・数分間隔）。
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
        <CreateGDriveForm onCreated={() => qc.invalidateQueries({ queryKey: ["connections"] })} />
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
                <th></th>
              </tr>
            </thead>
            <tbody>
              {items.map((c) => {
                const st = STATUS_LABEL[c.status] ?? { cls: "st-uploaded", label: c.status };
                const summary =
                  c.type === "gdrive"
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
                    <td>
                      {c.type === "gdrive" && (
                        <button
                          className="btn sm"
                          disabled={syncingId === c.id}
                          onClick={() => sync.mutate(c.id)}
                        >
                          {syncingId === c.id ? "同期中…" : "今すぐ同期"}
                        </button>
                      )}
                    </td>
                  </tr>
                );
              })}
              {items.length === 0 && (
                <tr>
                  <td colSpan={5} className="sub">
                    接続はまだありません。「＋ Google Drive を接続」でフォルダ監視を始められます。
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
      </p>
    </AppShell>
  );
}
