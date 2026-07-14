import { confClass } from "@/lib/fields";
import type { ExtractedField } from "@/lib/types";

// §2 CONFIDENCE BAR：色分け＋数値を常に併記（色だけに依存しない, §7 a11y）
export function ConfidenceBar({ field }: { field: ExtractedField }) {
  const cls = confClass(field);
  const pct = Math.round(field.confidence * 100);
  const num = field.confidence.toFixed(2);
  return (
    <span className={`conf ${cls}`} aria-label={`確信度 ${num}`}>
      <span className="bar" aria-hidden>
        <i style={{ width: `${pct}%` }} />
      </span>
      {num}
    </span>
  );
}
