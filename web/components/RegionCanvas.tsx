"use client";

// 領域指定キャンバス（設計 §3.4）。ページ画像の上に矩形を描く／選ぶ／消す。
//
// v1 の操作は「描く・選ぶ・消す・ゴースト確定・モード切替」だけ。移動と四隅リサイズは
// 入れない（再ドラッグでの置換で代替できるうえ、ハンドルの当たり判定は矩形が小さい
// ときに描画開始と競合する）。
//
// 表示は**ページ全体 fit**。スクロールできる大きな画像にすると、ドラッグ中に
// 端へ寄って自動スクロールが起きたときの座標補正が要り、そこがずれると
// 「見えている位置と保存される位置が違う」という最悪の不具合になる。
// 全体を収めてしまえばその経路自体が消える。

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

import { usePageImage } from "@/lib/usePageImage";

export type Px = [number, number, number, number];

export interface CanvasRegion {
  id: string;
  bbox: Px;
  kind: "include" | "exclude";
  label?: string;
}

export interface CanvasGhost {
  key: string;
  bbox: Px;
  label: string;
}

// クリックとドラッグの境目（表示 px）。これ未満は矩形にしない。
const MIN_DRAG_PX = 8;

export function RegionCanvas({
  documentId,
  pageNo,
  pageDim,
  regions,
  ghosts,
  mode,
  selectedId,
  onDraw,
  onSelect,
  onBackgroundDown,
  onGhostClick,
  onDelete,
}: {
  documentId: string;
  pageNo: number;
  pageDim: { width?: number | null; height?: number | null } | undefined;
  regions: CanvasRegion[];
  ghosts: CanvasGhost[];
  mode: "include" | "exclude";
  selectedId: string | null;
  onDraw: (bbox: Px) => void;
  onSelect: (id: string | null) => void;
  // 背景（何も無い所）を押したとき。**矩形の選択だけ**を外す。ここで行の選択まで
  // 消すと、「項目を選ぶ → その項目の領域をドラッグで描く」が成立しなくなる
  // （pointerdown で選択が消え、pointerup の時点では紐づけ先が無い）。
  onBackgroundDown: () => void;
  onGhostClick: (key: string) => void;
  onDelete: (id: string) => void;
}) {
  const { url, failed } = usePageImage(documentId, pageNo);
  const imgRef = useRef<HTMLImageElement>(null);
  const stageRef = useRef<HTMLDivElement>(null);
  const [scale, setScale] = useState(0);
  const [drag, setDrag] = useState<{ x0: number; y0: number; x1: number; y1: number } | null>(null);

  const W = pageDim?.width ?? 0;
  const H = pageDim?.height ?? 0;

  // 表示中の img が「今のページのもの」かを寸法で確かめる。url は非同期で届くので、
  // ページを切り替えた直後は前ページの img がまだ DOM に残り得る。縦横比の違う
  // ページへ切り替えた直後にドラッグされると、旧ページの scale で座標を作って
  // しまい、誤ページ・誤座標のまま保存される。
  const measure = useCallback(() => {
    const img = imgRef.current;
    if (!img || !W || !H) {
      setScale(0);
      return;
    }
    if (img.naturalWidth !== W || img.naturalHeight !== H) {
      setScale(0); // このページの画像ではない → 操作を受け付けない
      return;
    }
    setScale(img.clientWidth > 0 ? img.clientWidth / W : 0);
  }, [W, H]);

  // 描画のたびに測り直す。キャッシュ済み画像では load がレイアウト前に発火して
  // clientWidth が 0 になり、矩形が左上に潰れる（DocViewer で実際に踏んだ）。
  // setScale は同値なら React が再描画を止めるので、この形でループしない。
  useLayoutEffect(() => {
    measure();
  });

  useEffect(() => {
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, [measure]);

  // ページ切替でドラッグ中なら破棄（進行中の矩形が別ページに落ちない）
  useEffect(() => {
    setDrag(null);
  }, [pageNo]);

  const ready = scale > 0;

  function stagePoint(ev: React.PointerEvent): { x: number; y: number } {
    // 毎イベントで測り直す（レイアウトが動いてもずれない）
    const r = stageRef.current!.getBoundingClientRect();
    return { x: ev.clientX - r.left, y: ev.clientY - r.top };
  }

  function onPointerDown(ev: React.PointerEvent) {
    // ネイティブの画像ドラッグが始まると pointer capture 中でも pointercancel が
    // 飛び、進行中の矩形が消えたまま up が来ない（矩形が作れない）。
    ev.preventDefault();
    if (!ready || ev.button !== 0) return;
    const t = ev.target as HTMLElement;
    if (t.closest("[data-region]") || t.closest("[data-ghost]")) return; // 既存矩形の操作
    onBackgroundDown();
    const p = stagePoint(ev);
    (ev.currentTarget as HTMLElement).setPointerCapture(ev.pointerId);
    setDrag({ x0: p.x, y0: p.y, x1: p.x, y1: p.y });
  }

  function onPointerMove(ev: React.PointerEvent) {
    if (!drag) return;
    const p = stagePoint(ev);
    setDrag((d) => (d ? { ...d, x1: p.x, y1: p.y } : d));
  }

  function onPointerUp(ev: React.PointerEvent) {
    if (!drag) return;
    const { x0, y0, x1, y1 } = drag;
    setDrag(null);
    try {
      (ev.currentTarget as HTMLElement).releasePointerCapture(ev.pointerId);
    } catch {
      /* capture 済みでない場合は無視 */
    }
    // 逆方向ドラッグは min/max で吸収する
    const left = Math.min(x0, x1);
    const top = Math.min(y0, y1);
    const right = Math.max(x0, x1);
    const bottom = Math.max(y0, y1);
    if (right - left < MIN_DRAG_PX || bottom - top < MIN_DRAG_PX) return; // クリック扱い
    if (!ready) return;
    const clampX = (v: number) => Math.max(0, Math.min(W, Math.round(v / scale)));
    const clampY = (v: number) => Math.max(0, Math.min(H, Math.round(v / scale)));
    onDraw([clampX(left), clampY(top), clampX(right), clampY(bottom)]);
  }

  // Delete/Backspace で選択中の矩形を消す。**入力欄にフォーカスがあるときは無視**
  // （doc_type や項目名を編集中の Backspace で矩形が消えると復元手段が無い）。
  useEffect(() => {
    function onKey(ev: KeyboardEvent) {
      const typing =
        ev.target instanceof HTMLInputElement ||
        ev.target instanceof HTMLTextAreaElement ||
        ev.target instanceof HTMLSelectElement;
      if (typing) return;
      if (ev.key === "Escape") {
        // Esc は選択解除のみ。プレビュー自体は閉じない（描いた内容を失わせない）
        ev.stopPropagation();
        onSelect(null);
        return;
      }
      if ((ev.key === "Delete" || ev.key === "Backspace") && selectedId) {
        ev.preventDefault();
        onDelete(selectedId);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selectedId, onDelete, onSelect]);

  const box = (b: Px) => ({
    left: b[0] * scale,
    top: b[1] * scale,
    width: (b[2] - b[0]) * scale,
    height: (b[3] - b[1]) * scale,
  });

  return (
    <div className="rgn-canvas">
      <div
        ref={stageRef}
        className={`rgn-stage${ready ? "" : " loading"} mode-${mode}`}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={() => setDrag(null)}
        onDragStart={(e) => e.preventDefault()}
      >
        {url ? (
          <img
            ref={imgRef}
            src={url}
            alt={`ページ ${pageNo}`}
            draggable={false}
            onLoad={measure}
          />
        ) : (
          <div className="rgn-loading">
            {failed ? "ページ画像を読み込めませんでした" : "ページ画像を読み込み中…"}
          </div>
        )}

        {ready &&
          ghosts.map((g) => (
            <div
              key={g.key}
              data-ghost
              className="bbox bx-ghost"
              style={box(g.bbox)}
              onClick={(e) => {
                e.stopPropagation();
                onGhostClick(g.key);
              }}
              role="button"
              tabIndex={0}
              onKeyDown={(e) => e.key === "Enter" && onGhostClick(g.key)}
              aria-label={`${g.label} をこの位置の読取領域として確定`}
              title="クリックでこの位置を読取領域として確定"
            >
              <span className="tag">{g.label}</span>
            </div>
          ))}

        {ready &&
          regions.map((r) => (
            <div
              key={r.id}
              data-region
              className={`bbox ${r.kind === "include" ? "bx-include" : "bx-exclude"}${
                selectedId === r.id ? " sel" : ""
              }`}
              style={box(r.bbox)}
              onPointerDown={(e) => {
                e.stopPropagation();
                onSelect(r.id);
              }}
              role="button"
              tabIndex={0}
              aria-label={`${r.kind === "include" ? "読取領域" : "除外領域"} ${r.label ?? ""}`}
            >
              <span className="tag">{r.label ?? (r.kind === "include" ? "読取" : "除外")}</span>
              <button
                className="rgn-x"
                aria-label="この領域を削除"
                onPointerDown={(e) => e.stopPropagation()}
                onClick={(e) => {
                  e.stopPropagation();
                  onDelete(r.id);
                }}
              >
                ×
              </button>
            </div>
          ))}

        {drag && (
          <div
            className={`bbox ${mode === "include" ? "bx-include" : "bx-exclude"} drawing`}
            style={{
              left: Math.min(drag.x0, drag.x1),
              top: Math.min(drag.y0, drag.y1),
              width: Math.abs(drag.x1 - drag.x0),
              height: Math.abs(drag.y1 - drag.y0),
            }}
          />
        )}
      </div>
    </div>
  );
}
