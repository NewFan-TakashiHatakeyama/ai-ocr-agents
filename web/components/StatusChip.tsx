import { type StatusKind, statusView } from "@/lib/statusLabels";

// §2 STATUS CHIPS。kind は必須: documents / extraction_runs / workflows で語彙が違うので、
// どの表で引くかを呼び出し側が明示する（documents の表の流用で superseded 等が生値で出ていた）。
export function StatusChip({ kind, status }: { kind: StatusKind; status: string }) {
  const { cls, label, hint } = statusView(kind, status);
  return (
    <span className={`chip ${cls}`} title={hint}>
      {label}
    </span>
  );
}
