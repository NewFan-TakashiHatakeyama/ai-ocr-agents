// 検証画面のローカル編集バッファ（§8.3: 編集バッファ=zustand）。

import { create } from "zustand";

interface ReviewState {
  selectedField: string | null;
  edits: Record<string, string>; // field_name -> corrected_value
  select: (fieldName: string | null) => void;
  setEdit: (fieldName: string, value: string) => void;
  clearEdits: () => void;
}

export const useReviewStore = create<ReviewState>((set) => ({
  selectedField: null,
  edits: {},
  select: (fieldName) => set({ selectedField: fieldName }),
  setEdit: (fieldName, value) =>
    set((s) => ({ edits: { ...s.edits, [fieldName]: value } })),
  clearEdits: () => set({ edits: {} }),
}));
