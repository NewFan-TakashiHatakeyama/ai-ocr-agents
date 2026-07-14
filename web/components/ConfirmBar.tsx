"use client";

import { useState } from "react";

import { api } from "@/lib/api";
import { useReviewStore } from "@/lib/store";
import type { ResultResponse } from "@/lib/types";

// §8.3 <ConfirmBar>: 残pending数、[全て確認して確定]。pending>0時は二段確認。

export function ConfirmBar({ result, onDone }: { result: ResultResponse; onDone: () => void }) {
  const { edits, clearEdits } = useReviewStore();
  const [busy, setBusy] = useState(false);
  const pending = Number(result.review_summary?.pending ?? 0);

  async function submit() {
    setBusy(true);
    try {
      // 変更バッファを corrections として保存（楽観ロック: result_version）
      const items = Object.entries(edits).map(([field_name, corrected_value]) => {
        const original = result.fields.find((f) => f.name === field_name);
        return {
          field_name,
          original_value: original?.value_normalized ?? null,
          corrected_value,
        };
      });
      if (items.length > 0) {
        await api.postCorrections(result.document_id, result.run_id, result.result_version, items);
      }
      // 確定（グラフ resume）
      await api.confirm(result.document_id, result.run_id);
      clearEdits();
      onDone();
    } finally {
      setBusy(false);
    }
  }

  async function onClick() {
    if (pending > 0 && !window.confirm(`未確認が ${pending} 件あります。確定してよいですか？`)) {
      return;
    }
    await submit();
  }

  return (
    <div className="confirm-bar">
      <span>
        確定 {Number(result.review_summary?.auto ?? 0)} ・ 要確認 {pending} ・ 編集{" "}
        {Object.keys(edits).length}
      </span>
      <button className="btn" onClick={onClick} disabled={busy || result.status === "confirmed"}>
        {busy ? "処理中…" : "全て確認して確定"}
      </button>
    </div>
  );
}
