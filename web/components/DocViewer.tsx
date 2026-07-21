"use client";

import { useEffect, useRef, useState } from "react";

import { api } from "@/lib/api";
import { bboxClass, confClass } from "@/lib/fields";
import { useReviewStore } from "@/lib/store";
import type { ExtractedField } from "@/lib/types";

// §8.3 <DocViewer>: 前処理後PNG＋bboxオーバーレイ。
// 座標変換 display = bbox × (renderedWidth / page.width)（前処理後画像=座標系の正, DD-01）。
// 色＝フィールド状態、ラベル＝フィールド名＋conf、クリックで右パネル該当行を選択（双方向同期）。

export function DocViewer({
  documentId,
  fields,
  pageNo,
}: {
  documentId: string;
  fields: ExtractedField[];
  pageNo: number;
}) {
  const [url, setUrl] = useState<string | null>(null);
  const [scale, setScale] = useState(1);
  const imgRef = useRef<HTMLImageElement>(null);
  const { selectedField, select, selectedCell } = useReviewStore();

  useEffect(() => {
    let active = true;
    api
      .pageImage(documentId, pageNo)
      .then((r) => active && setUrl(r.url))
      .catch(() => active && setUrl(null));
    return () => {
      active = false;
    };
  }, [documentId, pageNo]);

  function onLoad() {
    const img = imgRef.current;
    if (img && img.naturalWidth > 0) setScale(img.clientWidth / img.naturalWidth);
  }

  return (
    <div className="viewer">
      <div className="v-canvas">
        <div className="viewer-stage">
          {url ? (
            <img ref={imgRef} src={url} alt={`page ${pageNo}`} onLoad={onLoad} />
          ) : (
            <p style={{ padding: 40 }}>ページ画像を読み込み中…</p>
          )}
          {fields
            .filter((f) => f.bbox && (f.page ?? 1) === pageNo)
            .map((f) => {
              const [x1, y1, x2, y2] = f.bbox as [number, number, number, number];
              const sel = selectedField === f.name;
              return (
                <div
                  key={f.name}
                  className={`bbox ${bboxClass(f)}${sel ? " sel" : ""}`}
                  style={{
                    left: x1 * scale,
                    top: y1 * scale,
                    width: (x2 - x1) * scale,
                    height: (y2 - y1) * scale,
                  }}
                  onClick={() => select(f.name)}
                  role="button"
                  aria-label={`${f.label ?? f.name} 確信度 ${f.confidence.toFixed(2)}`}
                >
                  <span className="tag">
                    {f.label ?? f.name} {confClass(f) === "ok" ? "✓" : f.confidence.toFixed(2)}
                  </span>
                </div>
              );
            })}
          {selectedCell && selectedCell.page === pageNo && (
            <div
              className="bbox bx-cell sel"
              style={{
                left: selectedCell.bbox[0] * scale,
                top: selectedCell.bbox[1] * scale,
                width: (selectedCell.bbox[2] - selectedCell.bbox[0]) * scale,
                height: (selectedCell.bbox[3] - selectedCell.bbox[1]) * scale,
              }}
              aria-label="選択した明細セル"
            >
              <span className="tag">明細セル</span>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
