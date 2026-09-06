"use client";

// スキーマレス抽出の結果からスキーマを作る「テンプレート化」（ADR-0006 / §5.5.1）。
//
// 設計の要点: テンプレートは帳票を取り込む「前」に書かせない。まず自動発見で
// 値を抽出し、実際に取れた項目を見ながら「どの項目を今後も抽出するか」を選んで
// スキーマ化する。空欄から項目名を発明させる従来の順序（③スキーマ登録→①取込）を
// 逆転させ、実物の値を根拠に定義させる。
//
// このファイルはボタンと開閉だけを持つ。項目の選択・領域の指定・保存は
// TemplatizePreview、保存後のフォロー（再抽出・旧版ワークフロー警告）は
// useSchemaSaved が担う。作成は PUT /schemas（create=true, admin）で、
// 同名 doc_type は E1005 で拒否される。

import { useState } from "react";

import { TemplatizePreview } from "@/components/TemplatizePreview";
import type { ExtractedField, PageDim } from "@/lib/types";
import { useSchemaSaved } from "@/lib/useSchemaSaved";

export function TemplatizeSchema({
  documentId,
  fields,
  pages,
  runStatus,
  readOnly,
  suggestedDocType,
  onCreated,
  onRefetch,
}: {
  documentId: string;
  fields: ExtractedField[];
  pages: PageDim[];
  runStatus: string;
  readOnly: boolean;
  suggestedDocType?: string | null;
  // 作成後の再抽出と「領域・項目を編集」の入口条件に id が要るため、doc_type だけで
  // なく schema id も返す（run.schema_id は作成後も null のままなので、これが
  // 無いと同じ帳票から二度と編集に入れない write-once になる）。
  onCreated: (r: { docType: string; schemaId: string }) => void;
  onRefetch: () => void;
}) {
  const [open, setOpen] = useState(false);
  const afterSave = useSchemaSaved({ documentId, runStatus, readOnly, onRefetch });

  return (
    <>
      <button className="btn sm primary" onClick={() => setOpen(true)}>
        🧩 テンプレート化
      </button>
      {open && (
        <TemplatizePreview
          documentId={documentId}
          fields={fields}
          pages={pages}
          mode="create"
          docType={suggestedDocType ?? ""}
          onClose={() => setOpen(false)}
          onSaved={(r) => {
            setOpen(false);
            onCreated({ docType: r.docType, schemaId: r.schemaId });
            afterSave(r, true);
          }}
        />
      )}
    </>
  );
}
