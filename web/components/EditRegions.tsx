"use client";

// 「領域・項目を編集」導線（設計 §3.1 編集モード）。
//
// テンプレート化は write-once にしない。作った当の帳票からも、リロード後も、
// 同じプレビューを開いて領域を直し、新しい版として保存できる必要がある。
//
// 編集対象の doc_type は **doc_type 起点** で決める。list_schemas は doc_type ごと
// 最新版しか返さず、run.schema_id は抽出時点の旧版であり得るため id 突合はできない。
// サーバが result API で返す schema_doc_type を第一に、作成直後は
// このセッションで作った doc_type を使う。どちらも取れないときはボタンを出さない
// （空のプリロードで保存すると既存の定義を空の新版で上書きしてしまう）。

import { useState } from "react";

import { TemplatizePreview } from "@/components/TemplatizePreview";
import type { ExtractedField, PageDim } from "@/lib/types";
import { useSchemaSaved } from "@/lib/useSchemaSaved";

export function EditRegions({
  documentId,
  fields,
  pages,
  runStatus,
  readOnly,
  docType,
  onRefetch,
}: {
  documentId: string;
  fields: ExtractedField[];
  pages: PageDim[];
  runStatus: string;
  readOnly: boolean;
  docType: string;
  onRefetch: () => void;
}) {
  const [open, setOpen] = useState(false);
  const afterSave = useSchemaSaved({ documentId, runStatus, readOnly, onRefetch });

  return (
    <>
      <button className="btn sm" onClick={() => setOpen(true)} title={`スキーマ ${docType} を編集`}>
        ✎ 領域・項目を編集
      </button>
      {open && (
        <TemplatizePreview
          documentId={documentId}
          fields={fields}
          pages={pages}
          mode="edit"
          docType={docType}
          onClose={() => setOpen(false)}
          onSaved={(r) => {
            setOpen(false);
            afterSave(r, false);
          }}
        />
      )}
    </>
  );
}
