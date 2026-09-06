"use client";

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

import { bboxClass, confClass } from "@/lib/fields";
import { usePageImage } from "@/lib/usePageImage";
import { useReviewStore } from "@/lib/store";
import type { ExtractedField, ResolvedRegion } from "@/lib/types";

// §8.3 <DocViewer>: 前処理後PNG＋bboxオーバーレイ。
// 座標変換 display = bbox × (renderedWidth / page.width)（前処理後画像=座標系の正, DD-01）。
// 色＝フィールド状態、ラベル＝フィールド名＋conf、クリックで右パネル該当行を選択（双方向同期）。

export function DocViewer({
  documentId,
  fields,
  pageNo,
  excludeRegions = [],
}: {
  documentId: string;
  fields: ExtractedField[];
  pageNo: number;
  // この run に適用された除外領域（**サーバでページ解決済み**）。表示専用で、
  // ここから編集はできない（編集はテンプレート化プレビュー側の責務）。
  excludeRegions?: ResolvedRegion[];
}) {
  const { url } = usePageImage(documentId, pageNo);
  // 0 = 未計測。計測できるまでオーバーレイを描かない（誤った縮尺で重ねない）
  const [scale, setScale] = useState(0);
  const [natural, setNatural] = useState<{ w: number; h: number } | null>(null);
  const imgRef = useRef<HTMLImageElement>(null);
  const { selectedField, select, selectedCell } = useReviewStore();

  // 縮尺は onLoad **だけ**では測れない。画像がキャッシュ済みだと load がレイアウト前に
  // 発火して clientWidth が 0 になり、全ての矩形が左上に潰れる（実機で踏んだ）。
  // ウィンドウ幅を変えたときも onLoad は再発火しないので、矩形が画像からずれたままになる。
  // そこで「描画のたび（useLayoutEffect）＋ load ＋ window resize」で測り直す。
  // 値が変わらないときは同じ参照を返して再描画を止める（無限ループ防止）。
  const measure = useCallback(() => {
    const img = imgRef.current;
    if (!img || img.naturalWidth <= 0 || img.clientWidth <= 0) return;
    setScale(img.clientWidth / img.naturalWidth);
    setNatural((prev) =>
      prev && prev.w === img.naturalWidth && prev.h === img.naturalHeight
        ? prev
        : { w: img.naturalWidth, h: img.naturalHeight },
    );
  }, []);

  // ページを切り替えたら、次のページの画像を測り直すまで何も描かない
  useEffect(() => {
    setScale(0);
    setNatural(null);
  }, [pageNo]);

  useLayoutEffect(() => {
    measure();
  });

  useEffect(() => {
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, [measure]);

  return (
    <div className="viewer">
      <div className="v-canvas">
        <div className="viewer-stage">
          {url ? (
            <img ref={imgRef} src={url} alt={`page ${pageNo}`} onLoad={measure} />
          ) : (
            <p style={{ padding: 40 }}>ページ画像を読み込み中…</p>
          )}
          {scale > 0 &&
            fields
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
          {natural &&
            scale > 0 &&
            excludeRegions
              .filter((r) => r.page_no === pageNo)
              .map((r, i) => (
                <div
                  key={`ex-${i}`}
                  className="bbox bx-exclude-view"
                  style={{
                    left: r.rect[0] * natural.w * scale,
                    top: r.rect[1] * natural.h * scale,
                    width: (r.rect[2] - r.rect[0]) * natural.w * scale,
                    height: (r.rect[3] - r.rect[1]) * natural.h * scale,
                  }}
                  title={`${r.label ?? "除外領域"}: 除外設定により未取込`}
                  aria-label={`${r.label ?? "除外領域"} — 除外設定により未取込`}
                >
                  <span className="tag">{r.label ?? "除外"}</span>
                </div>
              ))}
          {scale > 0 && selectedCell && selectedCell.page === pageNo && (
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
