import { statusChip } from "@/lib/fields";

// §2 STATUS CHIPS（documents.status と1:1）
export function StatusChip({ status }: { status: string }) {
  const { cls, label } = statusChip(status);
  return <span className={`chip ${cls}`}>{label}</span>;
}
