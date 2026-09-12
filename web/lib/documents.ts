// 帳票一覧（SCR-02）の表示名と検索の純粋関数。
//
// 一覧・レビューキュー・検証画面の見出しが同じ規則で名前を出すために切り出した。
// 規則を各画面に書くと「一覧はファイル名、キューは ID」のようにばらける。

import type { DocumentMeta } from "@/lib/types";

/**
 * 画面に出す帳票の名前。原本ファイル名があればそれ、無ければ document_id。
 * external_ref は名前ではなく外部システムの参照なのでここでは使わない
 * （一覧では別の列に出る）。
 */
export function documentDisplayName(
  d: Pick<DocumentMeta, "document_id" | "original_name">,
): string {
  const name = d.original_name?.trim();
  return name ? name : d.document_id;
}

/**
 * 一覧の検索（部分一致・大文字小文字を区別しない）。
 * 空のキーワードは全件に一致する。原本ファイル名・帳票 ID・種別・external_ref の
 * どれかに含まれれば一致。
 */
export function matchesDocumentQuery(
  d: Pick<DocumentMeta, "document_id" | "original_name" | "doc_type" | "external_ref">,
  query: string,
): boolean {
  const kw = query.trim().toLowerCase();
  if (!kw) return true;
  return [d.original_name, d.document_id, d.doc_type, d.external_ref].some((v) =>
    v?.toLowerCase().includes(kw),
  );
}
