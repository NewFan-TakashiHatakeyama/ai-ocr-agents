// チャット承認カード（SCR-01, §4.5）の純粋ロジック。
//
// SSE の confirm_request は `{ action, ...ツール引数, prompt }` の平坦なオブジェクト
// （gateway chat_graph.confirm_action_node）。承認時は action / prompt を除いた残りを
// **そのまま** params として POST /chat/confirm に返す。サーバ側の dto.Chat*Params が
// 名前を検証するので、ここで組み替えたり一部だけ拾ったりしてはいけない
// （以前は doc_type / field しか送らず、rerun_extract / manage_rules が必ず失敗した）。

export interface Confirm {
  action: string;
  prompt?: string;
  [key: string]: unknown;
}

/** POST /chat/confirm に送る形へ分解する。params は confirm_request の残り全部。 */
export function splitConfirm(c: Confirm): { action: string; params: Record<string, unknown> } {
  const { action, prompt: _prompt, ...params } = c;
  return { action, params };
}

function str(v: unknown): string | undefined {
  return typeof v === "string" && v ? v : undefined;
}

const RULE_OP_LABEL: Record<string, string> = { active: "有効化", retired: "退役" };

/**
 * 承認カードに出す「実際に何を送るか」の要約行。
 * LLM が書いた prompt とは別に、サーバへ渡る値そのものを見せる（承認の根拠）。
 */
export function describeConfirm(c: Confirm): string[] {
  switch (c.action) {
    case "update_schema": {
      const field = (c.field ?? {}) as Record<string, unknown>;
      const name = str(field.name) ?? "?";
      const label = str(field.label) ?? name;
      const type = str(field.type);
      return [`スキーマ: ${str(c.doc_type) ?? "invoice"}`, `項目: ${label}（${name}${type ? ` / ${type}` : ""}）`];
    }
    case "rerun_extract": {
      const lines = [`対象: ${str(c.document_id) ?? "?"}`, `スキーマ: ${str(c.schema_id) ?? "現在の設定"}`];
      lines.push(
        c.supersede_review === false
          ? "確認待ちの結果がある場合は実行しません"
          : "確認待ち（needs_review）の結果は置き換えます",
      );
      return lines;
    }
    case "manage_rules": {
      const status = str(c.status) ?? "?";
      return [`ルール: ${str(c.rule_id) ?? "?"}`, `操作: ${RULE_OP_LABEL[status] ?? status}`];
    }
    default:
      return [];
  }
}

/** 承認実行に要る権限（gateway chat_tools.WRITE_TOOL_MIN_ROLE と対応）。403 の文言に使う。 */
const ROLE_LABEL: Record<string, string> = {
  update_schema: "管理者権限",
  manage_rules: "管理者権限",
  rerun_extract: "アップロード以上の権限",
};

export function deniedMessage(action: string): string {
  const label = ROLE_LABEL[action];
  return label ? `この操作には${label}が必要です。` : "この操作を行う権限がありません。";
}

/** 実行成功後に開ける画面（detail はサーバの ChatConfirmResult.detail）。 */
export function resultLink(action: string, detail: Record<string, unknown>): { href: string; label: string } | null {
  switch (action) {
    case "update_schema":
      return { href: "/schemas", label: "スキーマを開く" };
    case "manage_rules":
      return { href: "/rules", label: "ルールを開く" };
    case "rerun_extract": {
      const id = str(detail.document_id);
      return id ? { href: `/documents/${id}`, label: "ドキュメントを開く" } : null;
    }
    default:
      return null;
  }
}
