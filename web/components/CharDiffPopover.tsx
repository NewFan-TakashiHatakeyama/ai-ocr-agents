"use client";

import { vChecks } from "@/lib/fields";
import type { ExtractedField } from "@/lib/types";

// §8.3 <CharDiffPopover>: 要確認フィールドの候補採択。
// [1]=OCR原値 / [2]=LLM補正案（推奨・根拠バッジ）/ [e]=手入力。DD-10 により候補は
// 混同表・修正メモリ由来のみ。根拠（混同ペア・V-SUM整合・memory_refs）を明示する。

interface Correction {
  to?: string;
  from?: string;
  rationale?: string;
  used_pairs?: string[];
  memory_refs?: string[];
}

export function CharDiffPopover({
  field,
  onSelect,
  onEdit,
}: {
  field: ExtractedField;
  onSelect: (value: string) => void;
  onEdit: () => void;
}) {
  const corr = (field.correction ?? {}) as Correction;
  const ocr = field.value_raw ?? field.value_normalized ?? "";
  const suggestion = corr.to;
  const checks = vChecks(field);

  return (
    <div className="cdp" role="group" aria-label={`${field.label ?? field.name} の候補`}>
      <button className="cdp-opt" onClick={() => onSelect(ocr)}>
        <span className="kbd">1</span>
        <span className="v mono">{ocr || "（空）"}</span>
        <span className="src ocr">OCR原値</span>
      </button>

      {suggestion && suggestion !== ocr && (
        <button className="cdp-opt rec" onClick={() => onSelect(suggestion)}>
          <span className="kbd">2</span>
          <span className="v mono">{suggestion}</span>
          <span className="src llm">LLM補正</span>
          {checks.map((c) => (
            <span key={c} className="vbadge">
              {c}
            </span>
          ))}
          <span className="cdp-rec">推奨</span>
        </button>
      )}

      <button className="cdp-opt ghost" onClick={onEdit}>
        <span className="kbd">e</span>
        <span style={{ color: "var(--ink3)" }}>手入力…</span>
      </button>

      {corr.rationale && (
        <div className="cdp-why">
          根拠: {corr.rationale}
          {corr.used_pairs && corr.used_pairs.length > 0 && (
            <>
              {" "}
              混同ペア <b>{corr.used_pairs.join(", ")}</b>
            </>
          )}
          {corr.memory_refs && corr.memory_refs.length > 0 && (
            <>
              {" "}
              類似修正 <code>{corr.memory_refs[0]}</code>
            </>
          )}
        </div>
      )}
    </div>
  );
}
