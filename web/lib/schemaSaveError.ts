// PUT /schemas の失敗をトースト文言に変える（SCR-06 スキーマ管理）。
//
// 純粋関数に切り出すのは、409 の意味が 1 つでないため。新規作成モードの 409 には
// 「同名が既に存在する（一覧から選び直せる）」と「同名がアーカイブ済み（一覧に**出て
// いない**ので選び直せない。C9-D）」があり、後者を前者の文言で潰すと利用者は
// 「既存スキーマを選んで」と言われながら画面のどこにも無い、という行き止まりになる。
// アーカイブ済みはサーバの文言（復元の案内）をそのまま出し、「アーカイブ済みを表示」
// へ誘導する。

/** gateway の ApiError と同じ形（instanceof は dev のモジュール重複で false になり得る） */
export interface SaveErrorLike {
  status?: number;
  message?: string;
  details?: Record<string, unknown>;
}

export interface SaveErrorToast {
  kind: "warn" | "err";
  message: string;
  /** 同名がアーカイブ済み。呼び出し側は「アーカイブ済みを表示」の操作を付ける */
  archived: boolean;
}

export function schemaSaveErrorToast(
  e: unknown,
  opts: { creating: boolean; docType: string },
): SaveErrorToast {
  const err = (e ?? {}) as SaveErrorLike;
  const fallback = `保存に失敗しました（${(e as Error)?.message ?? "不明なエラー"}）。`;
  if (err.status !== 409) return { kind: "err", message: fallback, archived: false };
  if (err.details?.archived === true) {
    return {
      kind: "warn",
      archived: true,
      message:
        err.message ||
        `スキーマ「${opts.docType}」はアーカイブ済みです。使うには先に復元してください（アーカイブ済みを表示 → 復元）`,
    };
  }
  if (opts.creating) {
    return {
      kind: "err",
      archived: false,
      message: "同名のスキーマが既に存在します。既存スキーマを選んで編集してください。",
    };
  }
  return { kind: "err", message: fallback, archived: false };
}
