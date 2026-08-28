"use client";

// スキーマレス抽出の結果からスキーマを作る「テンプレート化」（ADR-0006 / §5.5.1）。
//
// 設計の要点: テンプレートは帳票を取り込む「前」に書かせない。まず自動発見で
// 値を抽出し、実際に取れた項目を見ながら「どの項目を今後も抽出するか」を選んで
// スキーマ化する。空欄から項目名を発明させる従来の順序（③スキーマ登録→①取込）を
// 逆転させ、実物の値を根拠に定義させる。
//
// 作成は PUT /schemas（create=true, admin）。同名 doc_type は E1005 で拒否される。

import { useMemo, useState } from "react";

import { ApiError, api } from "@/lib/api";
import { useToasts } from "@/lib/toast";
import type { ExtractedField, SchemaFieldDto } from "@/lib/types";

// 値の見た目から正規化型（§5.6 FieldType）を推測する。あくまで初期値で、
// 利用者がダイアログで直せる。迷ったら string に倒す（誤った型は正規化で値を壊す）。
export function guessFieldType(value: string | null | undefined): string {
  if (!value) return "string";
  const t = value.trim();
  if (/^T\d{13}$/.test(t)) return "jp_invoice_reg_no";
  if (/^\d{1,2}(\.\d+)?\s?[%％]$/.test(t)) return "tax_rate_jp";
  // money は「値全体が金額の形」の時だけ。部分一致 /円/ にすると
  // 「円谷プロダクション株式会社」「渋谷区円山町」まで money_jpy になり、
  // そのまま保存されると次回抽出の正規化が値を破壊する（敵対的レビュー確定）
  if (/^[¥￥]?\s?-?\d{1,3}(,\d{3})*(\.\d+)?\s?円?$/.test(t) && /[¥￥,円]/.test(t))
    return "money_jpy";
  if (/^\d{4}[-/年.]\s?\d{1,2}([-/月.]\s?\d{1,2}\s?日?)?$/.test(t)) return "date";
  if (/^-?\d+(\.\d+)?$/.test(t.replace(/,/g, ""))) return "number";
  return "string";
}

const TYPE_OPTIONS = [
  ["string", "文字列"],
  ["date", "日付"],
  ["money_jpy", "金額(円)"],
  ["number", "数値"],
  ["tax_rate_jp", "税率"],
  ["jp_invoice_reg_no", "登録番号(T+13桁)"],
  ["jp_bank_account", "銀行口座"],
] as const;

type Draft = {
  include: boolean;
  name: string;
  label: string;
  type: string;
  sample: string;
};

export function TemplatizeSchema({
  fields,
  suggestedDocType,
  onCreated,
}: {
  fields: ExtractedField[];
  suggestedDocType?: string | null;
  onCreated: (docType: string) => void;
}) {
  const push = useToasts((s) => s.push);
  const [open, setOpen] = useState(false);
  const [docType, setDocType] = useState(suggestedDocType ?? "");
  const [busy, setBusy] = useState(false);
  const [drafts, setDrafts] = useState<Draft[]>([]);

  const init = useMemo(
    () =>
      fields.map((f) => {
        const sample = f.value_normalized ?? f.value_raw ?? "";
        return {
          include: Boolean(sample), // 値が取れなかった項目は既定で外す
          name: f.name,
          label: f.label ?? f.name,
          type: guessFieldType(sample),
          sample,
        };
      }),
    [fields],
  );

  function openDialog() {
    setDrafts(init);
    setOpen(true);
  }

  function patch(i: number, d: Partial<Draft>) {
    setDrafts((ds) => ds.map((x, j) => (j === i ? { ...x, ...d } : x)));
  }

  function issue(): string | null {
    const dt = docType.trim();
    if (!dt) return "帳票種別（doc_type）を入力してください。";
    const chosen = drafts.filter((d) => d.include);
    if (chosen.length === 0) return "抽出する項目を 1 つ以上選んでください。";
    const names = chosen.map((d) => d.name.trim());
    if (names.some((n) => !/^[A-Za-z][A-Za-z0-9_]*$/.test(n)))
      return "項目名（name）は英字始まりの英数字・アンダースコアにしてください。";
    if (new Set(names).size !== names.length) return "項目名（name）が重複しています。";
    return null;
  }

  async function save() {
    const problem = issue();
    if (problem) {
      push({ kind: "warn", message: problem });
      return;
    }
    const body: SchemaFieldDto[] = drafts
      .filter((d) => d.include)
      .map((d) => ({
        name: d.name.trim(),
        label: d.label.trim() || d.name.trim(),
        type: d.type,
        required: false,
        critical: false,
      }));
    setBusy(true);
    try {
      await api.putSchema(docType.trim(), body, { create: true });
      push({
        kind: "ok",
        message: `スキーマ「${docType.trim()}」を作成しました。次回からこの項目で抽出できます。`,
      });
      setOpen(false);
      onCreated(docType.trim());
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        push({ kind: "warn", message: "同名のスキーマが既にあります。別の名前にするか、スキーマ管理で編集してください。" });
      } else if (e instanceof ApiError && e.status === 403) {
        push({ kind: "warn", message: "スキーマの作成には管理者権限が必要です。" });
      } else {
        push({ kind: "err", message: `作成できませんでした（${(e as Error).message}）。` });
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <button className="btn sm primary" onClick={openDialog}>
        🧩 テンプレート化
      </button>
      {open && (
        <div className="tpl-overlay" role="dialog" aria-modal="true" aria-label="テンプレート化">
          <div className="tpl-card">
            <h3>この抽出結果をテンプレート化</h3>
            <p className="sub">
              残す項目を選んで保存すると、同じ種類の帳票を次回から
              この定義で自動抽出できます（スキーマ v1 として登録）。
              明細（表）は対象外です — 結果画面でそのまま確認できます。
            </p>
            <label className="extract-field">
              <span>帳票種別（doc_type）</span>
              <input
                value={docType}
                onChange={(e) => setDocType(e.target.value)}
                placeholder="例: invoice, quotation, 納品書"
                autoFocus
              />
            </label>
            <div className="tpl-rows">
              <div className="tpl-row tpl-head">
                <span />
                <span>表示名</span>
                <span>項目名（name）</span>
                <span>型</span>
                <span>今回の値</span>
              </div>
              {drafts.map((d, i) => (
                <div className={`tpl-row${d.include ? "" : " off"}`} key={i}>
                  <input
                    type="checkbox"
                    checked={d.include}
                    onChange={(e) => patch(i, { include: e.target.checked })}
                    aria-label={`${d.label} を含める`}
                  />
                  <input value={d.label} onChange={(e) => patch(i, { label: e.target.value })} />
                  <input
                    className="mono"
                    value={d.name}
                    onChange={(e) => patch(i, { name: e.target.value })}
                  />
                  <select value={d.type} onChange={(e) => patch(i, { type: e.target.value })}>
                    {TYPE_OPTIONS.map(([v, l]) => (
                      <option key={v} value={v}>
                        {l}
                      </option>
                    ))}
                  </select>
                  <span className="sub" title={d.sample}>
                    {d.sample || "—"}
                  </span>
                </div>
              ))}
            </div>
            <div className="tpl-actions">
              <button className="btn" onClick={() => setOpen(false)} disabled={busy}>
                キャンセル
              </button>
              <button className="btn grad" onClick={save} disabled={busy}>
                {busy ? "作成中…" : "スキーマを作成"}
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
