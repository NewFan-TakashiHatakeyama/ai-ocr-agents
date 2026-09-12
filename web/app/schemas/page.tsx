"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useEffect, useState } from "react";

import { AppShell } from "@/components/AppShell";
import { StatusChip } from "@/components/StatusChip";
import { ApiError, api } from "@/lib/api";
import { chatHrefForSchema, schemaAddRequest } from "@/lib/schemaChat";
import { schemaSaveErrorToast } from "@/lib/schemaSaveError";
import { useToasts } from "@/lib/toast";
import type { SchemaDto, SchemaFieldDto, WorkflowRefDto } from "@/lib/types";

// SCR-06 スキーマ管理（§5.5）。座標は登場せず、意味定義（名前・型・重要度）だけを版管理。
const TYPES = ["string", "money_jpy", "date", "number", "jp_invoice_reg_no", "tax_rate_jp", "table"];

/** 409(E1005) の details.workflows を「名前」の並びにする。無ければ空文字 */
function workflowNames(details?: Record<string, unknown>): string {
  const rows = (details?.workflows as WorkflowRefDto[] | undefined) ?? [];
  return rows.map((w) => `「${w.name}」`).join("、");
}

// アーカイブ / 復元の確認文言。アーカイブは元に戻せる（復元）が、一覧・抽出のスキーマ
// 選択・ワークフローの候補から消えることは先に言う（C9-D）。
function confirmArchive(s: SchemaDto): boolean {
  return window.confirm(
    `スキーマ「${s.doc_type}」（v${s.version}）をアーカイブします。\n` +
      "抽出のスキーマ選択・帳票種別の候補・ワークフローの候補から消え、新しい版も作れなくなります。" +
      "過去の抽出結果はそのまま残り、「復元」で元に戻せます。\n\nよろしいですか？",
  );
}

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
  // 「アーカイブ済みを表示」（C9-D）。既定 off。key の第 2 要素で分けるので、他画面の
  // ["schemas"]（候補一覧・既定＝隠す）とキャッシュが混ざらない。invalidate は prefix 一致
  const [showArchived, setShowArchived] = useState(false);
  const { data, error, isLoading } = useQuery({
    queryKey: ["schemas", { includeArchived: showArchived }],
    queryFn: () => api.listSchemas({ includeArchived: showArchived }),
  });
  const [docType, setDocType] = useState<string | null>(null);
  const [fields, setFields] = useState<SchemaFieldDto[]>([]);
  // 新規スキーマ作成モード（③）: 既存 doc_type の編集ではなく、ゼロから項目を定義する。
  const [creating, setCreating] = useState(false);
  const [newDocType, setNewDocType] = useState("");

  // docType が指定済みで一覧に見つからない場合は current=undefined（保存ボタンが無効化される）。
  // `?? items[0]` へ落とすと、作成直後の stale な一覧の間「別スキーマの見出し＋新規項目」で
  // 保存でき、無関係なスキーマを新規項目で上書きできてしまう（敵対的レビュー確定major）
  const current = creating
    ? undefined
    : docType
      ? data?.items.find((s) => s.doc_type === docType)
      : data?.items[0];
  useEffect(() => {
    if (!creating && current) {
      setDocType(current.doc_type);
      setFields(current.fields.map((f) => ({ ...f })));
    }
  }, [data]); // eslint-disable-line react-hooks/exhaustive-deps

  const save = useMutation({
    mutationFn: () =>
      api.putSchema(creating ? newDocType.trim() : current!.doc_type, fields, { create: creating }),
    onSuccess: (rec) => {
      push({
        kind: "ok",
        message: creating
          ? `スキーマ「${rec.doc_type}」を作成しました（v${rec.version}）。抽出のスキーマ選択に表示されます。`
          : `新しい版として保存しました（${rec.doc_type} v${rec.version}）。進行中のRunには影響しません。`,
      });
      // サーバ応答を正として項目を同期（一覧 refetch を待たない）
      setFields(rec.fields.map((f) => ({ ...f })));
      setCreating(false);
      setDocType(rec.doc_type);
      qc.invalidateQueries({ queryKey: ["schemas"] });
      // 一覧の種別セレクト（GET /doc-types）も新しい種別・版を反映する
      qc.invalidateQueries({ queryKey: ["doc-types"] });
    },
    onError: (e) => {
      const docTypeTried = creating ? newDocType.trim() : (current?.doc_type ?? "");
      const t = schemaSaveErrorToast(e, { creating, docType: docTypeTried });
      if (!t.archived) {
        push({ kind: t.kind, message: t.message });
        return;
      }
      // 同名がアーカイブ済み（C9-D）。既定の一覧に出ていないので「既存を選んで」では
      // 行き止まり。サーバの文言（復元の案内）に、その場で「アーカイブ済みを表示」して
      // 該当スキーマ（復元ボタン付き）を開く操作を付ける
      push({
        kind: t.kind,
        message: t.message,
        action: {
          label: "アーカイブ済みを表示",
          onClick: () => {
            setShowArchived(true);
            setCreating(false);
            setDocType(docTypeTried);
          },
        },
      });
    },
  });

  // アーカイブ / 復元。有効なワークフローが使っていれば 409（先に停止してもらう）
  const archive = useMutation({
    mutationFn: (v: { s: SchemaDto; archived: boolean }) =>
      v.archived ? api.archiveSchema(v.s.doc_type) : api.unarchiveSchema(v.s.doc_type),
    onSuccess: (rec) => {
      push({
        kind: "ok",
        message: rec.archived
          ? `「${rec.doc_type}」をアーカイブしました。「アーカイブ済みを表示」から復元できます。`
          : `「${rec.doc_type}」を復元しました。抽出のスキーマ選択に再び表示されます。`,
      });
      qc.invalidateQueries({ queryKey: ["schemas"] });
      qc.invalidateQueries({ queryKey: ["doc-types"] });
    },
    onError: (e, v) => {
      // instanceof は dev のモジュール重複で false になり得るため status/details を直接見る
      const err = e as { status?: number; details?: Record<string, unknown> };
      if (err.status === 409) {
        push({
          kind: "warn",
          message:
            `「${v.s.doc_type}」は有効なワークフロー ${workflowNames(err.details)} が使っているためアーカイブできません。` +
            "先にワークフローを停止してください。",
        });
        return;
      }
      push({
        kind: "err",
        message: `${v.archived ? "アーカイブ" : "復元"}できませんでした（${(e as Error).message}）。`,
      });
    },
  });

  if (error instanceof ApiError && error.status === 403) return <AdminDenied message="権限がありません" />;

  // アーカイブ済みは読めるだけ。編集（新版）はサーバも E1005 で断るので、押させない
  const readOnly = !creating && Boolean(current?.archived);

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
        {!creating && current && !current.archived && <StatusChip status="confirmed" />}
        {!creating && current && current.archived && (
          <span className="chip st-failed">アーカイブ済み</span>
        )}
        {!creating && current && (
          <span className="sub">
            v{current.version} · {current.archived ? "アーカイブ済み" : "有効"}
          </span>
        )}
        {creating && <span className="sub">まだ保存されていません</span>}
        <span className="spacer" />
        {creating && (
          <button className="btn sm ghost" disabled={save.isPending} onClick={cancelCreate}>
            キャンセル
          </button>
        )}
        {!creating && current && !current.archived && (
          <button
            className="btn sm ghost"
            disabled={archive.isPending || save.isPending}
            title="一覧と候補から外す（復元できます）"
            onClick={() => {
              if (confirmArchive(current)) archive.mutate({ s: current, archived: true });
            }}
          >
            {archive.isPending ? "処理中…" : "アーカイブ"}
          </button>
        )}
        {!creating && current && current.archived && (
          <button
            className="btn sm"
            disabled={archive.isPending}
            title="一覧と候補に戻す"
            onClick={() => archive.mutate({ s: current, archived: false })}
          >
            {archive.isPending ? "処理中…" : "復元"}
          </button>
        )}
        <button
          className="btn sm primary"
          disabled={(creating ? false : !current) || save.isPending || readOnly}
          title={readOnly ? "アーカイブ済みのスキーマは編集できません。先に復元してください" : undefined}
          onClick={onSave}
        >
          {saveLabel}
        </button>
      </div>

      <div style={{ padding: "12px 22px", display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
        {data?.items.map((s) => (
          <button
            key={s.doc_type}
            className={`filter${!creating && s.doc_type === current?.doc_type ? " on" : ""}`}
            style={{
              ...(!creating && s.doc_type === current?.doc_type
                ? { borderColor: "var(--brand)", color: "var(--brand-deep)" }
                : {}),
              ...(s.archived ? { opacity: 0.6, textDecoration: "line-through" } : {}),
            }}
            title={s.archived ? "アーカイブ済み" : undefined}
            onClick={() => {
              setCreating(false);
              setDocType(s.doc_type);
              setFields(s.fields.map((f) => ({ ...f })));
            }}
          >
            {s.doc_type}（v{s.version}）{s.archived ? " 📦" : ""}
          </button>
        ))}
        <button
          className={`filter${creating ? " on" : ""}`}
          style={creating ? { borderColor: "var(--brand)", color: "var(--brand-deep)" } : undefined}
          onClick={startCreate}
        >
          ＋ 新規スキーマ
        </button>
        <span className="spacer" />
        <label className="sub" style={{ display: "flex", alignItems: "center", gap: 6, cursor: "pointer" }}>
          <input
            type="checkbox"
            checked={showArchived}
            onChange={(e) => setShowArchived(e.target.checked)}
            aria-label="アーカイブ済みを表示"
          />
          アーカイブ済みを表示
        </label>
      </div>

      {readOnly && (
        <p className="sub" style={{ padding: "0 22px" }}>
          📦 このスキーマはアーカイブ済みです。抽出のスキーマ選択・ワークフローの候補には出ません。
          編集するには「復元」してください（過去の抽出結果はそのまま残っています）。
        </p>
      )}

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
                {f.region && (
                  // 読取領域と例示値（前回その位置にあった値）は隠さずに出す。何がヒントに
                  // 使われるかを人が見られる状態にする（設計 region-field-add-and-hint-v2 §2.3）。
                  // 編集はテンプレート化画面（画像を見ながら）で行う。
                  <span
                    className="sub"
                    title={
                      `読取領域: p.${String(f.region.page ?? "全")}` +
                      (f.region.origin ? ` / ${f.region.origin === "ghost" ? "AI の位置" : "手描き"}` : "") +
                      (f.region.example_value ? `
例示値: ${f.region.example_value}` : "")
                    }
                  >
                    📐 読取領域あり
                    {f.region.example_value ? `（例示値: ${f.region.example_value.slice(0, 24)}${f.region.example_value.length > 24 ? "…" : ""}）` : ""}
                  </span>
                )}
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
          {/* SCR-01 のエージェント経由（update_schema）。チャットの承認カードを通して
              新版が作られる経路は実装済みなので、「準備中」で押せなくしたままにしない。
              **開いているスキーマの doc_type をリンクに載せる**: 素の /chat に送ると
              チャット側は対象を invoice に倒すので、delivery_note を見ながら頼んだ項目が
              invoice の新版になる。新規作成中（まだ無いスキーマ）には出さない */}
          {!creating && current && (
            <Link
              className="btn sm ghost"
              href={chatHrefForSchema(current.doc_type)}
              title={`チャットに「${schemaAddRequest(current.doc_type)}」のように頼むと、承認のうえ ${current.doc_type} の新版が作られます`}
            >
              💬 チャットで追加を依頼
            </Link>
          )}
        </div>
        <p className="sub" style={{ marginTop: 10 }}>
          型は正規化器レジストリ（§5.6）から選択。並び順は表示順のみ（抽出結果に影響しません）。保存は常に新版作成（§7.2）。
        </p>
      </div>
    </AppShell>
  );
}
