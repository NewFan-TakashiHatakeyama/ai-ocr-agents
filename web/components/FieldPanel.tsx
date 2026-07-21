"use client";

import { useState } from "react";

import { AuditBadge, VerifyBadges } from "@/components/AuditBadge";
import { CharDiffPopover } from "@/components/CharDiffPopover";
import { ConfidenceBar } from "@/components/ConfidenceBar";
import { TableGrid } from "@/components/TableGrid";
import { sortFields } from "@/lib/fields";
import { useReviewStore } from "@/lib/store";
import type { ExtractedField, TableResult } from "@/lib/types";

// §8.3 <FieldPanel>: 要確認→確定済みのセクション。由来バッジ・検証バッジ・conf帯。
// 選択中は右パネル行とビューアbboxを双方向同期。編集はバッファ（確定時に保存）。

function Row({ f, readOnly = false }: { f: ExtractedField; readOnly?: boolean }) {
  const { selectedField, edits, select, setEdit } = useReviewStore();
  const active = selectedField === f.name;
  const edited = f.name in edits;
  const current = edits[f.name] ?? f.value_normalized ?? f.value_raw ?? "";
  const pend = f.review_status === "pending";

  return (
    <>
      <div
        id={`frow-${f.name}`}
        className={`frow ${pend ? "pend" : "done"}${active ? " sel" : ""}`}
        onClick={() => select(f.name)}
      >
        <span className="fl">{f.label ?? f.name}</span>
        {active && !readOnly ? (
          <input
            className="fv"
            value={current}
            onChange={(e) => setEdit(f.name, e.target.value)}
            aria-label={`${f.label ?? f.name} を編集`}
            autoFocus
          />
        ) : (
          <span className="fv" title={current}>
            {current || <em style={{ color: "var(--ink3)" }}>（空）</em>}
          </span>
        )}
        {edited && <span className="src human">編集</span>}
        {!edited && <AuditBadge field={f} />}
        <VerifyBadges field={f} />
        <ConfidenceBar field={f} />
      </div>
      {active && pend && !readOnly && (
        <div className="cdp-wrap">
          <CharDiffPopover field={f} onSelect={(v) => setEdit(f.name, v)} onEdit={() => {}} />
        </div>
      )}
    </>
  );
}

export function FieldPanel({
  fields,
  tables = [],
  readOnly = false,
}: {
  fields: ExtractedField[];
  tables?: TableResult[];
  readOnly?: boolean;
}) {
  const [onlyPending, setOnlyPending] = useState(false);
  const sorted = sortFields(fields);
  const pending = sorted.filter((f) => f.review_status === "pending");
  const done = sorted.filter((f) => f.review_status !== "pending");

  return (
    <div className="fpanel">
      <div className="fp-head">
        フィールド
        <span className="spacer" />
        <button
          className="filter"
          style={{ cursor: "pointer" }}
          onClick={() => setOnlyPending((v) => !v)}
          aria-pressed={onlyPending}
        >
          {onlyPending ? "すべて表示" : "要確認のみ"}
        </button>
      </div>

      {pending.length > 0 && <div className="fp-sec">要確認（{pending.length}）</div>}
      {pending.map((f) => (
        <Row key={f.name} f={f} readOnly={readOnly} />
      ))}

      {!onlyPending && done.length > 0 && <div className="fp-sec">確定済み（{done.length}）</div>}
      {!onlyPending && done.map((f) => <Row key={f.name} f={f} readOnly={readOnly} />)}

      {!onlyPending && <TableGrid tables={tables} />}
    </div>
  );
}
