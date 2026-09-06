// トースト（§2 TOAST / 409 楽観ロック競合通知）。エラーは「何が起きたか＋次の一手」。

import { create } from "zustand";

export type ToastKind = "info" | "ok" | "warn" | "err";

export interface Toast {
  id: number;
  kind: ToastKind;
  message: string;
  action?: { label: string; onClick: () => void };
}

interface ToastState {
  toasts: Toast[];
  push: (t: Omit<Toast, "id">) => void;
  dismiss: (id: number) => void;
}

let seq = 0;

export const useToasts = create<ToastState>((set) => ({
  toasts: [],
  push: (t) => {
    const id = ++seq;
    set((s) => ({ toasts: [...s.toasts, { ...t, id }] }));
    // **操作を伴うトーストは自動で消さない**。選択肢を出しておきながら 5 秒で
    // 引っ込めると、読み終わる前に押す機会が消える（実機の QA で「この帳票を
    // 再抽出」を押せずに流れた）。warn も従来どおり残す。
    if (t.kind !== "warn" && !t.action) {
      setTimeout(() => set((s) => ({ toasts: s.toasts.filter((x) => x.id !== id) })), 5000);
    }
  },
  dismiss: (id) => set((s) => ({ toasts: s.toasts.filter((x) => x.id !== id) })),
}));
