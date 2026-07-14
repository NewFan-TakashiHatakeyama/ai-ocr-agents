"use client";

import type { TableResult } from "@/lib/types";

// §8.3 <TableGridEditor>: 明細テーブルの表示。列＝行キーの和集合、セル＝value。
// セル選択で該当 span をビューアに連携（span_ids）。編集/行操作/列型検証は今後拡張。

function columnsOf(t: TableResult): string[] {
  const cols: string[] = [];
  for (const row of t.rows) {
    for (const k of Object.keys(row)) if (!cols.includes(k)) cols.push(k);
  }
  return cols;
}

export function TableGrid({ tables }: { tables: TableResult[] }) {
  if (!tables || tables.length === 0) return null;
  return (
    <div className="fp-sec-block">
      {tables.map((t, ti) => {
        const cols = columnsOf(t);
        return (
          <div key={ti} style={{ padding: "6px 14px 14px" }}>
            <div className="fp-sec" style={{ padding: "6px 0 6px", background: "transparent" }}>
              明細: {t.name}（{t.rows.length}行）
              {t.confidence != null && <span className="sub"> · conf {t.confidence.toFixed(2)}</span>}
            </div>
            <div className="gridedit">
              <table>
                <thead>
                  <tr>
                    {cols.map((c) => (
                      <th key={c}>{c}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {t.rows.map((row, ri) => (
                    <tr key={ri}>
                      {cols.map((c) => (
                        <td key={c} title={(row[c]?.span_ids ?? []).join(",")}>
                          {row[c]?.value ?? ""}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        );
      })}
    </div>
  );
}
