// フィールドの状態導出（§8.3 / §2 トークン）。confidence 色分け・由来(AuditBadge)・並び順。

import type { ExtractedField } from "./types";

// confidence 帯: lo <0.60 / mid 0.60–0.79 / hi ≥0.80 / ok=検証合格(auto-elevation, ≥0.98 or 決定論合格)
export function confClass(f: ExtractedField): "lo" | "mid" | "hi" | "ok" {
  if (f.validation?.passed || f.confidence >= 0.98) return "ok";
  if (f.confidence < 0.6) return "lo";
  if (f.confidence < 0.8) return "mid";
  return "hi";
}

// bbox 色（フィールド状態）: 琥珀=要確認 / 青=検証合格 / 赤=低確信 / 緑=良好
export function bboxClass(f: ExtractedField): string {
  if (f.review_status === "pending") return "bx-pend";
  if (f.validation?.passed || f.confidence >= 0.98) return "bx-val";
  if (f.confidence < 0.6) return "bx-lo";
  return "bx-ok";
}

export interface AuditSource {
  key: "ocr" | "llm" | "rule" | "human" | "vl";
  label: string;
}

// 値の由来（§11 監査要件）。データから最善で導出する。
export function sourceOf(f: ExtractedField): AuditSource {
  if (f.review_status === "corrected") return { key: "human", label: "人手修正" };
  const corr = f.correction as { source?: string; changed?: boolean } | null | undefined;
  if (corr && (corr.changed ?? true)) {
    if (corr.source === "rule") return { key: "rule", label: "ルール適用" };
    return { key: "llm", label: "LLM補正" };
  }
  return { key: "ocr", label: "OCR原値" };
}

// 検証バッジ（V-SUM 等）。validation.checks を採用。
export function vChecks(f: ExtractedField): string[] {
  if (f.validation?.passed && f.validation.checks) return f.validation.checks;
  return [];
}

// 並び: pending → その他。pending 内は conf 昇順（危険を先頭に）。
export function sortFields(fields: ExtractedField[]): ExtractedField[] {
  return [...fields].sort((a, b) => {
    const ap = a.review_status === "pending" ? 0 : 1;
    const bp = b.review_status === "pending" ? 0 : 1;
    if (ap !== bp) return ap - bp;
    return a.confidence - b.confidence;
  });
}

// documents.status → chip クラス/表示名（§2 STATUS CHIPS, 1:1）
const STATUS_MAP: Record<string, { cls: string; label: string }> = {
  uploaded: { cls: "st-uploaded", label: "アップロード済" },
  queued: { cls: "st-queued", label: "処理待ち" },
  processing: { cls: "st-processing", label: "処理中" },
  needs_review: { cls: "st-review", label: "要確認" },
  in_review: { cls: "st-inreview", label: "確認中" },
  confirmed: { cls: "st-confirmed", label: "確定" },
  exported: { cls: "st-exported", label: "連携済" },
  failed: { cls: "st-failed", label: "失敗" },
};

export function statusChip(status: string): { cls: string; label: string } {
  return STATUS_MAP[status] ?? { cls: "st-uploaded", label: status };
}
