// 大量処理（複数アップロード・一括再抽出）の純粋関数（設計 bulk-processing §3）。
//
// 画面（documents/page.tsx・useSchemaSaved）から切り出してあるのは、文言と数え方を
// 1 箇所に置くため。「何件投入して何件がなぜ落ちたか」を 2 つの導線で別々に組むと、
// 片方だけ確定済みの件数を落とす、といった食い違いがすぐ起きる。DOM には触らない。

import type { DocTypeDto, ExtractBatchResponse, ExtractBatchSkipped } from "./types";

// ---- アップロードするファイルの選別 ----

/** ingest が受ける MIME（documents/page.tsx の accept 属性と対） */
export const ACCEPTED_MIME = ["application/pdf", "image/png", "image/jpeg", "image/tiff"] as const;
/** ブラウザが MIME を空で返すことがある（拡張子の紐付けが無い環境）ので拡張子でも見る */
export const ACCEPTED_EXT = [".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"] as const;

export interface FileLike {
  name: string;
  type: string;
}

export function isAcceptedFile(f: FileLike): boolean {
  if ((ACCEPTED_MIME as readonly string[]).includes(f.type)) return true;
  const lower = f.name.toLowerCase();
  return ACCEPTED_EXT.some((ext) => lower.endsWith(ext));
}

/**
 * 取り込めるものと除外するものに分ける。**投げる前に**除外して件数を伝えるため。
 * サーバに投げてから E1001 で返ってくると、他のファイルの成否と混ざって読めない。
 */
export function partitionFiles<T extends FileLike>(files: Iterable<T>): {
  accepted: T[];
  rejected: T[];
} {
  const accepted: T[] = [];
  const rejected: T[] = [];
  for (const f of files) (isAcceptedFile(f) ? accepted : rejected).push(f);
  return { accepted, rejected };
}

/** 種別の最新版 schema_id（GET /doc-types の一覧から）。無ければ undefined */
export function latestSchemaId(
  docTypes: readonly DocTypeDto[] | undefined,
  docType: string,
): string | undefined {
  return docTypes?.find((t) => t.doc_type === docType)?.schema_id || undefined;
}

// ---- アップロードの要約 ----

export interface UploadTally {
  /** アップロードに成功した件数 */
  ok: number;
  /** アップロードに失敗した件数 */
  failed: number;
  /** 取り込めない形式で投げる前に除外した件数 */
  rejected: number;
  /** アップロード後に抽出を開始できた件数 */
  extractStarted: number;
  /** アップロードは成功したが抽出を開始できなかった件数 */
  extractFailed: number;
}

export function summarizeUploads(t: UploadTally): { kind: "ok" | "warn"; message: string } {
  const parts: string[] = [];
  if (t.ok > 0) parts.push(`${t.ok} 件をアップロードしました。`);
  else if (t.failed > 0) parts.push(`アップロードに失敗しました（${t.failed} 件）。`);
  if (t.extractStarted > 0) parts.push(`${t.extractStarted} 件の抽出を開始しました。`);
  if (t.extractFailed > 0) {
    parts.push(
      `${t.extractFailed} 件は抽出を開始できませんでした（帳票ページから開始できます）。`,
    );
  }
  if (t.ok > 0 && t.failed > 0) parts.push(`${t.failed} 件は失敗しました。`);
  if (t.rejected > 0) {
    parts.push(`${t.rejected} 件は取り込めない形式（PDF / PNG / JPEG / TIFF 以外）のため除外しました。`);
  }
  const clean = t.failed === 0 && t.extractFailed === 0 && t.rejected === 0;
  return { kind: clean ? "ok" : "warn", message: parts.join("") };
}

// ---- 一括再抽出の要約 ----

export type SkipReason = "confirmed" | "busy" | "no_schema" | "not_found" | "other";

const SKIP_LABEL: Record<SkipReason, string> = {
  confirmed: "確定済み",
  busy: "処理中",
  no_schema: "スキーマなし",
  not_found: "見つからない",
  other: "その他",
};

/**
 * skipped の理由を分類する。E1005 は「確定済み」と「処理中（競合）」の両方に使われ、
 * 応答の details は一括では返らないので、サーバの文言（「確定済みの結果があります」）で
 * 見分ける。文言を変えるときはここも直す。
 */
export function classifySkip(s: ExtractBatchSkipped): SkipReason {
  if (s.code === "no_schema") return "no_schema";
  if (s.code === "E1001") return "not_found";
  if (s.code === "E1005") return s.message.includes("確定") ? "confirmed" : "busy";
  return "other";
}

export function countSkips(skipped: readonly ExtractBatchSkipped[]): Partial<Record<SkipReason, number>> {
  const out: Partial<Record<SkipReason, number>> = {};
  for (const s of skipped) {
    const r = classifySkip(s);
    out[r] = (out[r] ?? 0) + 1;
  }
  return out;
}

/** 「N 件を再抽出に投入しました（M 件はスキップ: 確定済み 2 / スキーマなし 1）」 */
export function summarizeBatch(res: ExtractBatchResponse): {
  kind: "ok" | "warn";
  message: string;
} {
  const n = res.accepted.length;
  const m = res.skipped.length;
  const counts = countSkips(res.skipped);
  const breakdown = (Object.keys(SKIP_LABEL) as SkipReason[])
    .filter((r) => (counts[r] ?? 0) > 0)
    .map((r) => `${SKIP_LABEL[r]} ${counts[r]}`)
    .join(" / ");
  let message: string;
  if (n === 0 && m === 0) {
    message = "対象の帳票がありませんでした。";
  } else if (n === 0) {
    message = `再抽出に投入できませんでした（${m} 件はスキップ: ${breakdown}）。`;
  } else if (m === 0) {
    message = `${n} 件を再抽出に投入しました。`;
  } else {
    message = `${n} 件を再抽出に投入しました（${m} 件はスキップ: ${breakdown}）。`;
  }
  if (res.truncated) {
    message += "新しい順に 200 件で打ち切りました。残りはもう一度実行してください。";
  }
  return { kind: n > 0 && m === 0 && !res.truncated ? "ok" : "warn", message };
}

// ---- 一覧の選択・進行中の検知 ----

/** 抽出が動いている documents.status。一覧の自動再取得（5 秒）の条件 */
export const RUNNING_STATUSES: readonly string[] = ["queued", "processing"];

export function countRunning(items: readonly { status: string }[]): number {
  return items.filter((d) => RUNNING_STATUSES.includes(d.status)).length;
}

/**
 * 選択した帳票のうち種別のある件数・無い件数。無い分は一括ではスキップされる
 * （no_schema）ので、確認ダイアログで先に伝える。
 */
export function partitionSelection(
  items: readonly { document_id: string; doc_type?: string | null }[],
  selected: ReadonlySet<string>,
): { withType: number; withoutType: number } {
  let withType = 0;
  let withoutType = 0;
  for (const d of items) {
    if (!selected.has(d.document_id)) continue;
    if (d.doc_type) withType += 1;
    else withoutType += 1;
  }
  return { withType, withoutType };
}
