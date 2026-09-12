// スキーマ管理（SCR-06）→ チャット（SCR-01）への「この schema に項目を足して」の受け渡し。
//
// チャットの update_schema は doc_type をメッセージから決める（rule-based は
// 「<doc_type> のスキーマに…」を読み、LLM は文意から取る。どちらも書いていなければ
// invoice に倒す）。スキーマ管理から素の /chat に送ると、開いていたスキーマが何で
// あれ invoice の新版（無ければ 1 項目だけの新規 invoice）になる。だからリンクに
// doc_type を載せ、チャット側は入力欄をその doc_type 入りの依頼文で始める。

/** スキーマ管理からチャットへ渡すクエリパラメータ名 */
export const CHAT_DOC_TYPE_PARAM = "doc_type";

/** 「チャットで追加を依頼」のリンク先。doc_type は URL エンコードして載せる */
export function chatHrefForSchema(docType: string): string {
  return `/chat?${CHAT_DOC_TYPE_PARAM}=${encodeURIComponent(docType)}`;
}

/**
 * チャットの入力欄に最初から入れる依頼文。「<doc_type> のスキーマに「<項目>」を追加して」。
 * 項目名は例（利用者が書き換える前提）。doc_type が空なら対象を書かない従来の文。
 */
export function schemaAddRequest(docType: string | null | undefined, label = "支払方法"): string {
  const dt = docType?.trim();
  return dt ? `${dt} のスキーマに「${label}」を追加して` : `スキーマに「${label}」を追加して`;
}
