import { sourceOf, vChecks } from "@/lib/fields";
import type { ExtractedField } from "@/lib/types";

// §2 由来バッジ（AuditBadge）＋検証バッジ。値の由来は必ず可視化（§11 監査）。
export function AuditBadge({ field }: { field: ExtractedField }) {
  const src = sourceOf(field);
  const title =
    (field.correction as { rationale?: string } | null)?.rationale ??
    `由来: ${src.label}`;
  return (
    <span className={`src ${src.key}`} title={title}>
      {src.label}
    </span>
  );
}

export function VerifyBadges({ field }: { field: ExtractedField }) {
  const checks = vChecks(field);
  if (checks.length === 0) return null;
  return (
    <>
      {checks.map((c) => (
        <span key={c} className="vbadge" title={`決定論チェック合格: ${c}`}>
          {c}
        </span>
      ))}
    </>
  );
}
