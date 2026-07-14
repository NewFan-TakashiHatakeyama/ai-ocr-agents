"use client";

import { useToasts } from "@/lib/toast";

// aria-live=polite（確定完了は assertive）。色だけに依存しない（§7）。
export function Toaster() {
  const { toasts, dismiss } = useToasts();
  return (
    <div className="toast-wrap" aria-live="polite">
      {toasts.map((t) => (
        <div
          key={t.id}
          className={`toast ${t.kind === "info" ? "" : t.kind}`}
          role={t.kind === "err" ? "alert" : "status"}
        >
          <span>{t.message}</span>
          {t.action && (
            <button
              className="btn sm"
              onClick={() => {
                t.action?.onClick();
                dismiss(t.id);
              }}
            >
              {t.action.label}
            </button>
          )}
          <button className="btn sm ghost" aria-label="閉じる" onClick={() => dismiss(t.id)}>
            ×
          </button>
        </div>
      ))}
    </div>
  );
}
