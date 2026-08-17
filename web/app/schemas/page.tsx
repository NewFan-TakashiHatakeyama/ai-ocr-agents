"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { AppShell } from "@/components/AppShell";
import { StatusChip } from "@/components/StatusChip";
import { ApiError, api } from "@/lib/api";
import { useToasts } from "@/lib/toast";
import type { SchemaFieldDto } from "@/lib/types";

// SCR-06 スキーマ管理（§5.5）。座標は登場せず、意味定義（名前・型・重要度）だけを版管理。
const TYPES = ["string", "money_jpy", "date", "number", "jp_invoice_reg_no", "tax_rate_jp", "table"];

function AdminDenied({ message }: { message: string }) {
  return (
    <AppShell active="schemas">
      <div className="topbar">
        <span className="ttl">スキーマ管理</span>
      </div>
      <div className="access-denied">
        <div style={{ fontSize: 30 }}>🔒</div>
        <h3>{message}</h3>
        <p>この画面は管理者（admin）のみ利用できます。</p>
      </div>
    </AppShell>
  );
}

export default function SchemasPage() {
  const qc = useQueryClient();
  const push = useToasts((s) => s.push);
  const { data, error, isLoading } = useQuery({ queryKey: ["schemas"], queryFn: () => api.listSchemas() });
  const [docType, setDocType] = useState<string | null>(null);
  const [fields, setFields] = useState<SchemaFieldDto[]>([]);
  // 新規スキーマ作成モード（③）: 既存 doc_type の編集ではなく、ゼロから項目を定義する。
  const [creating, setCreating] = useState(false);
  const [newDocType, setNewDocType] = useState("");

  const current = creating ? undefined : data?.items.find((s) => s.doc_type === docType) ?? data?.items[0];
  useEffect(() => {
    if (!creating && current) {
      setDocType(current.doc_type);
      setFields(current.fields.map((f) => ({ ...f })));
    }
  }, [data]); // eslint-disable-line react-hooks/exhaustive-deps

  const save = useMutation({
    mutationFn: () => api.putSchema(creating ? newDocType.trim() : current!.doc_type, fields),
    onSuccess: (rec) => {
      push({
        kind: "ok",
        message: creating
          ? `スキーマ「${rec.doc_type}」を作成しました（v${rec.version}）。抽出のスキーマ選択に表示されます。`
          : `新しい版として保存しました（${rec.doc_type} v${rec.version}）。進行中のRunには影響しません。`,
      });
      setCreating(false);
      setDocType(rec.doc_type);
      qc.invalidateQueries({ queryKey: ["schemas"] });
    },
    onError: (e) => push({ kind: "err", message: `保存に失敗しました（${(e as Error).message}）。` }),
  });

  if (error instanceof ApiError && error.status === 403) return <AdminDenied message="権限がありません" />;

  function setField(i: number, patch: Partial<SchemaFieldDto>) {
    setFields((fs) => fs.map((f, j) => (j === i ? { ...f, ...patch } : f)));
  }

  function startCreate() {
    setCreating(true);
    setNewDocType("");
    setFields([]);
  }

  function cancelCreate() {
    setCreating(false);
    // 既存選択に戻す（useEffect が current から fields を復元）
    const first = data?.items.find((s) => s.doc_type === docType) ?? data?.items[0];
    if (first) {
      setDocType(first.doc_type);
      setFields(first.fields.map((f) => ({ ...f })));
    }
  }

  // 新規作成時のみ課す検証。既存編集は従来どおり（name は不変・版管理で担保）。
  function createIssue(): string | null {
    const dt = newDocType.trim();
    if (!dt) return "帳票タイプ名（doc_type）を入力してください。";
    if (data?.items.some((s) => s.doc_type === dt))
      return `「${dt}」は既に存在します。別名にするか、既存スキーマを選んで編集してください。`;
    if (fields.length === 0) return "項目を1つ以上追加してください。";
    const names = fields.map((f) => (f.name ?? "").trim());
    if (names.some((n) => !n)) return "すべての項目に項目名（name）を付けてください。";
    if (names.some((n) => !/^[A-Za-z][\w]*$/.test(n)))
      return "項目名（name）は半角英字で始まる英数字・_ のみで指定してください（例: total_amount）。";
    const dup = names.find((n, i) => names.indexOf(n) !== i);
    if (dup) return `項目名「${dup}」が重複しています。`;
    return null;
  }

  function onSave() {
    if (creating) {
      const issue = createIssue();
      if (issue) {
        push({ kind: "warn", message: issue });
        return;
      }
    }
    save.mutate();
  }

  const saveLabel = creating
    ? save.isPending
      ? "作成中…"
      : "スキーマを作成"
    : save.isPending
      ? "保存中…"
      : `新しい版として保存${current ? `（v${current.version + 1}）` : ""}`;

  return (
    <AppShell active="schemas">
      <div className="topbar">
        <span className="ttl">
          スキーマ{creating ? ": 新規作成" : current ? `: ${current.doc_type}` : ""}
        </span>
        {!creating && current && <StatusChip status="confirmed" />}
        {!creating && current && <span className="sub">v{current.version} · 有効</span>}
        {creating && <span className="sub">まだ保存されていません</span>}
        <span className="spacer" />
        {creating && (
          <button className="btn sm ghost" disabled={save.isPending} onClick={cancelCreate}>
            キャンセル
          </button>
        )}
        <button
          className="btn sm primary"
          disabled={(creating ? false : !current) || save.isPending}
          onClick={onSave}
        >
          {saveLabel}
        </button>
      </div>

      <div style={{ padding: "12px 22px", display: "flex", gap: 8, flexWrap: "wrap" }}>
        {data?.items.map((s) => (
          <button
            key={s.doc_type}
            className={`filter${!creating && s.doc_type === current?.doc_type ? " on" : ""}`}
            style={
              !creating && s.doc_type === current?.doc_type
                ? { borderColor: "var(--brand)", color: "var(--brand-deep)" }
                : undefined
            }
            onClick={() => {
              setCreating(false);
              setDocType(s.doc_type);
              setFields(s.fields.map((f) => ({ ...f })));
            }}
          >
            {s.doc_type}（v{s.version}）
          </button>
        ))}
        <button
          className={`filter${creating ? " on" : ""}`}
          style={creating ? { borderColor: "var(--brand)", color: "var(--brand-deep)" } : undefined}
          onClick={startCreate}
        >
          ＋ 新規スキーマ
        </button>
      </div>

      <div style={{ padding: "4px 22px 22px" }}>
        {isLoading && <p>読み込み中…</p>}

        {creating && (
          <div className="schema-newhead">
            <label>
              帳票タイプ名（doc_type）
              <input
                className="schema-in"
                value={newDocType}
                onChange={(e) => setNewDocType(e.target.value)}
                placeholder="例: purchase_order / 発注書"
                autoFocus
                aria-label="帳票タイプ名"
              />
            </label>
            <span className="sub">
              抽出時のスキーマ選択に表示されます。既存の名前とは重複できません。
            </span>
          </div>
        )}

        {!creating && current && fields.length === 0 && (
          <p className="sub">項目がありません。「＋ 項目を追加」で定義します。</p>
        )}
        {creating && fields.length === 0 && (
          <p className="sub">「＋ 項目を追加」で、抽出したい項目を1つずつ定義します。</p>
        )}

        {fields.map((f, i) => (
          <div key={i} className="srow">
            {creating ? (
              <span className="nm nm-edit">
                <input
                  className="schema-in"
                  value={f.name}
                  onChange={(e) => setField(i, { name: e.target.value })}
                  placeholder="項目名 name（例: total_amount）"
                  aria-label={`項目${i + 1} の name`}
                />
                <input
                  className="schema-in sub-in"
                  value={f.label ?? ""}
                  onChange={(e) => setField(i, { label: e.target.value })}
                  placeholder="表示名（日本語・任意）"
                  aria-label={`項目${i + 1} の表示名`}
                />
              </span>
            ) : (
              <span className="nm">
                {f.label || f.name || "（無題）"}
                <span className="sub">{f.name}</span>
              </span>
            )}
            <select
              className="typepill"
              value={f.type}
              onChange={(e) => setField(i, { type: e.target.value })}
              aria-label={`${f.name} の型`}
            >
              {TYPES.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
            <label onClick={() => setField(i, { required: !f.required })}>
              <span className={`tgl${f.required ? " on" : ""}`} />
              必須
            </label>
            <label
              onClick={() => setField(i, { critical: !f.critical })}
              title="critical=ON で閾値0.90＋レビュー方針を厳格化"
            >
              <span className={`tgl${f.critical ? " on" : ""}`} />
              critical
            </label>
            <span className="spacer" />
            <button className="btn sm ghost" onClick={() => setFields((fs) => fs.filter((_, j) => j !== i))}>
              削除
            </button>
          </div>
        ))}

        <div style={{ display: "flex", gap: 10, marginTop: 6 }}>
          <button
            className="btn sm"
            onClick={() =>
              setFields((fs) => [
                ...fs,
                { name: `field_${fs.length + 1}`, label: "", type: "string", required: false, critical: false },
              ])
            }
          >
            ＋ 項目を追加
          </button>
          <button className="btn sm ghost" title="SCR-01 のエージェント経由（update_schema）。準備中" disabled>
            💬 チャットで追加を依頼
          </button>
        </div>
        <p className="sub" style={{ marginTop: 10 }}>
          型は正規化器レジストリ（§5.6）から選択。並び順は表示順のみ（抽出結果に影響しません）。保存は常に新版作成（§7.2）。
        </p>
      </div>
    </AppShell>
  );
}
