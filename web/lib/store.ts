// 検証画面のローカル編集バッファ（§8.3: 編集バッファ=zustand）。

import { create } from "zustand";

import type { BBox } from "./types";

export interface CellSelection {
  bbox: BBox;
  page: number;
  key: string; // セル識別（テーブル行×列）
}

interface ReviewState {
  selectedField: string | null;
  selectedCell: CellSelection | null; // §8.3 明細セル↔ビューア連携
  edits: Record<string, string>; // field_name -> corrected_value
  select: (fieldName: string | null) => void;
  selectCell: (cell: CellSelection | null) => void;
  setEdit: (fieldName: string, value: string) => void;
  clearEdits: () => void;
}

export const useReviewStore = create<ReviewState>((set) => ({
  selectedField: null,
  selectedCell: null,
  edits: {},
  select: (fieldName) => set({ selectedField: fieldName, selectedCell: null }),
  selectCell: (cell) => set({ selectedCell: cell, selectedField: null }),
  setEdit: (fieldName, value) =>
    set((s) => ({ edits: { ...s.edits, [fieldName]: value } })),
  clearEdits: () => set({ edits: {} }),
}));
