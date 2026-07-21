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

  const current = data?.items.find((s) => s.doc_type === docType) ?? data?.items[0];
  useEffect(() => {
    if (current) {
      setDocType(current.doc_type);
      setFields(current.fields.map((f) => ({ ...f })));
    }
  }, [data]); // eslint-disable-line react-hooks/exhaustive-deps

  const save = useMutation({
    mutationFn: () => api.putSchema(current!.doc_type, fields),
    onSuccess: (rec) => {
      push({ kind: "ok", message: `新しい版として保存しました（${rec.doc_type} v${rec.version}）。進行中のRunには影響しません。` });
      qc.invalidateQueries({ queryKey: ["schemas"] });
    },
    onError: (e) => push({ kind: "err", message: `保存に失敗しました（${(e as Error).message}）。` }),
  });

  if (error instanceof ApiError && error.status === 403) return <AdminDenied message="権限がありません" />;

  function setField(i: number, patch: Partial<SchemaFieldDto>) {
    setFields((fs) => fs.map((f, j) => (j === i ? { ...f, ...patch } : f)));
  }

  return (
    <AppShell active="schemas">
      <div className="topbar">
        <span className="ttl">スキーマ{current ? `: ${current.doc_type}` : ""}</span>
        {current && <StatusChip status="confirmed" />}
        {current && <span className="sub">v{current.version} · 有効</span>}
        <span className="spacer" />
        <button className="btn sm primary" disabled={!current || save.isPending} onClick={() => save.mutate()}>
          {save.isPending ? "保存中…" : `新しい版として保存${current ? `（v${current.version + 1}）` : ""}`}
        </button>
      </div>

      <div style={{ padding: "12px 22px", display: "flex", gap: 8, flexWrap: "wrap" }}>
        {data?.items.map((s) => (
          <button
            key={s.doc_type}
            className={`filter${s.doc_type === current?.doc_type ? " on" : ""}`}
            style={s.doc_type === current?.doc_type ? { borderColor: "var(--brand)", color: "var(--brand-deep)" } : undefined}
            onClick={() => setDocType(s.doc_type)}
          >
            {s.doc_type}（v{s.version}）
          </button>
        ))}
      </div>

      <div style={{ padding: "4px 22px 22px" }}>
        {isLoading && <p>読み込み中…</p>}
        {current && fields.length === 0 && <p className="sub">項目がありません。「＋ 項目を追加」で定義します。</p>}
        {fields.map((f, i) => (
          <div key={i} className="srow">
            <span className="nm">
              {f.label || f.name || "（無題）"}
              <span className="sub">{f.name}</span>
            </span>
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
              setFields((fs) => [...fs, { name: `field_${fs.length + 1}`, label: "", type: "string", required: false, critical: false }])
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
