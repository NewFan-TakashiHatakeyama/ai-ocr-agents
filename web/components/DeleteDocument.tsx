"use client";

// 取り込んだ帳票の削除ボタン（§6.2 DELETE /documents/{id}）。
//
// 一覧（SCR-02）と検証画面（SCR-03）の両方から使う。導線が 2 つある以上、
// 確認文言とエラーの読み替えを 1 箇所に集約しないと、片方だけ「何が消えるか
// 書いていない」状態になりやすい。
//
// 復元手段は無い（ゴミ箱も Undo も無い）。だから確認文言には必ず
// ファイル名と「何が一緒に消えるか」を入れる。

import { useMutation } from "@tanstack/react-query";

import { api } from "@/lib/api";
import { hasRole, usePrincipal } from "@/lib/principal";
import { useToasts } from "@/lib/toast";

export function DeleteDocument({
  documentId,
  label,
  disabled,
  className = "btn sm danger",
  onDeleted,
}: {
  documentId: string;
  /** 確認ダイアログに出す表示名（原本ファイル名など）。無ければ ID を使う */
  label?: string | null;
  disabled?: boolean;
  className?: string;
  onDeleted: () => void;
}) {
  const push = useToasts((s) => s.push);
  const { me, ready } = usePrincipal();

  const del = useMutation({
    mutationFn: () => api.deleteDocument(documentId),
    onSuccess: (r) => {
      const extra = r.corrections_deleted > 0 ? `・学習例 ${r.corrections_deleted} 件` : "";
      push({ kind: "ok", message: `削除しました（ファイル ${r.objects_deleted} 件${extra}）。` });
      onDeleted();
    },
    onError: (e) => {
      // instanceof は dev のモジュール重複で false になり得るため status/details を直接見る。
      const err = e as { status?: number; details?: Record<string, unknown> };
      if (err.status === 409) {
        // 409 は「今は消せない」だけで、時間をおけば通る。恒久エラーと区別して伝える。
        const reason = String(err.details?.reason ?? "");
        const holder = String(err.details?.holder ?? "他のメンバー");
        push({
          kind: "warn",
          message:
            reason === "locked"
              ? `${holder} が確認中のため削除できません。`
              : reason === "workflow_active"
                ? "ワークフローの実行が終わるまで削除できません。"
                : // 停止したまま固着した処理は時間で見切られる（既定 30 分）。
                  // 「少し待って」と書くと、実際には数十分待たされて嘘になる。
                  "処理中のため削除できません。完了後、または処理が停止している場合は" +
                  "しばらく経ってから再試行してください。",
        });
        return;
      }
      if (err.status === 403) {
        // gateway は認証失敗（トークン期限切れ・不正）と権限不足のどちらも
        // E5001→403 で返す。ロールで見分けて、原因を取り違えないようにする。
        push({
          kind: "warn",
          message: hasRole(me.role, "reviewer")
            ? "削除できませんでした。サインインし直してください（トークンが無効な可能性があります）。"
            : "削除する権限がありません（reviewer 以上が必要です）。",
        });
        return;
      }
      push({ kind: "err", message: `削除できませんでした（${(e as Error).message}）。` });
    },
  });

  function confirmThenDelete() {
    const name = label || documentId;
    const ok = window.confirm(
      `「${name}」を削除します。\n` +
        "抽出結果・原本ファイル・この帳票から学習した修正例も消え、元に戻せません。\n\n" +
        "よろしいですか？",
    );
    if (ok) del.mutate();
  }

  // 権限が無い利用者にボタンを見せない。押させてから 403 で断るのは、不可逆操作の
  // 確認ダイアログに「はい」を押させた後で拒否することになり最悪の順序。
  // ready 前に消すとマウント直後にちらつくので、判定が出るまでは出さない。
  if (!ready || !hasRole(me.role, "reviewer")) return null;

  return (
    <button
      className={className}
      disabled={disabled || del.isPending}
      onClick={confirmThenDelete}
      title="この帳票を削除する（元に戻せません）"
    >
      {del.isPending ? "削除中…" : "削除"}
    </button>
  );
}
