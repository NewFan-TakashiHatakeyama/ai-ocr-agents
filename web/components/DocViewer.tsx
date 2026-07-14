"use client";

import { useEffect, useRef, useState } from "react";

import { api } from "@/lib/api";
import { useReviewStore } from "@/lib/store";
import type { ExtractedField } from "@/lib/types";

// §8.3 <DocViewer>: 前処理後PNG＋bboxオーバーレイ。座標変換は
// scale = renderedWidth / naturalWidth（前処理後画像=座標系の正, DD-01）。

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
  const selected = useReviewStore((s) => s.selectedField);

  useEffect(() => {
    let active = true;
    api
      .pageImage(documentId, pageNo)
      .then((r) => {
        if (active) setUrl(r.url);
      })
      .catch(() => setUrl(null));
    return () => {
      active = false;
    };
  }, [documentId, pageNo]);

  function onLoad() {
    const img = imgRef.current;
    if (img && img.naturalWidth > 0) {
      setScale(img.clientWidth / img.naturalWidth);
    }
  }

  return (
    <div className="viewer">
      <div className="viewer-stage">
        {url ? (
          <img ref={imgRef} src={url} alt={`page ${pageNo}`} onLoad={onLoad} />
        ) : (
          <p>ページ画像を読み込み中…</p>
        )}
        {fields
          .filter((f) => f.bbox && (f.page ?? 1) === pageNo)
          .map((f) => {
            const [x1, y1, x2, y2] = f.bbox as [number, number, number, number];
            return (
              <div
                key={f.name}
                className={`bbox${selected === f.name ? " selected" : ""}`}
                style={{
                  left: x1 * scale,
                  top: y1 * scale,
                  width: (x2 - x1) * scale,
                  height: (y2 - y1) * scale,
                }}
                aria-label={f.label ?? f.name}
              />
            );
          })}
      </div>
    </div>
  );
}
