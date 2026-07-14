"use client";

import { useReviewStore } from "@/lib/store";
import type { TableResult } from "@/lib/types";

// §8.3 <TableGridEditor>: 明細テーブルの表示。列＝行キーの和集合、セル＝value。
// セル選択で該当セル bbox をビューアにハイライト（構造由来 cell_box, §5.3 grounding）。

function columnsOf(t: TableResult): string[] {
  const cols: string[] = [];
  for (const row of t.rows) {
    for (const k of Object.keys(row)) if (!cols.includes(k)) cols.push(k);
  }
  return cols;
}

export function TableGrid({ tables }: { tables: TableResult[] }) {
  const { selectedCell, selectCell } = useReviewStore();
  if (!tables || tables.length === 0) return null;
  return (
    <div className="fp-sec-block">
      {tables.map((t, ti) => {
        const cols = columnsOf(t);
        const page = t.page ?? 1;
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
                      {cols.map((c) => {
                        const cell = row[c];
                        const key = `${ti}:${ri}:${c}`;
                        const on = selectedCell?.key === key;
                        return (
                          <td
                            key={c}
                            className={cell?.bbox ? `cell-clickable${on ? " cell-on" : ""}` : undefined}
                            onClick={() =>
                              cell?.bbox && selectCell({ bbox: cell.bbox, page, key })
                            }
                            title={cell?.bbox ? "クリックで画像の該当セルをハイライト" : undefined}
                          >
                            {cell?.value ?? ""}
                          </td>
                        );
                      })}
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
