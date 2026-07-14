"use client";

import { useReviewStore } from "@/lib/store";
import type { ExtractedField } from "@/lib/types";

// §8.3 <FieldPanel>: pending→critical→conf昇順。conf帯は色分け
// (<0.6赤 / <0.8黄 / >=0.8緑 / 検証済み青)。

function confColor(f: ExtractedField): string {
  if (f.validation?.passed) return "var(--validated)";
  if (f.confidence < 0.6) return "var(--low)";
  if (f.confidence < 0.8) return "var(--pending)";
  return "var(--ok)";
}

function sortFields(fields: ExtractedField[]): ExtractedField[] {
  return [...fields].sort((a, b) => {
    const ap = a.review_status === "pending" ? 0 : 1;
    const bp = b.review_status === "pending" ? 0 : 1;
    if (ap !== bp) return ap - bp;
    return a.confidence - b.confidence;
  });
}

export function FieldPanel({ fields }: { fields: ExtractedField[] }) {
  const { selectedField, edits, select, setEdit } = useReviewStore();

  return (
    <div className="field-panel">
      {sortFields(fields).map((f) => {
        const active = selectedField === f.name;
        const current = edits[f.name] ?? f.value_normalized ?? f.value_raw ?? "";
        return (
          <div
            key={f.name}
            className={`field-row${active ? " active" : ""}`}
            onClick={() => select(f.name)}
          >
            <div className="field-label">
              {f.label ?? f.name}
              {f.review_status === "pending" && <span className="badge pending">要確認</span>}
            </div>
            <div className="field-value">
              {active ? (
                <input
                  value={current}
                  onChange={(e) => setEdit(f.name, e.target.value)}
                  aria-label={`${f.name} を編集`}
                />
              ) : (
                <span>{current || <em style={{ color: "var(--muted)" }}>（空）</em>}</span>
              )}
            </div>
            <div className="conf-bar" style={{ background: confColor(f), width: `${Math.round(f.confidence * 100)}%` }} />
          </div>
        );
      })}
    </div>
  );
}
