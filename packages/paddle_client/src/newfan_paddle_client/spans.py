"""スパン統合（詳細設計 §5.3.3）。

- rec_polys（4点ポリゴン）→ 軸平行 bbox へ変換。
- 読み順: layout block 順 → ブロック内で上→下、左→右。
- span_id は Run 内で一意な連番（呼び出し側が start_id を渡して跨ページで連番化）。
"""

from __future__ import annotations

from newfan_schemas import LayoutBlock, Span, SpanSource

from newfan_paddle_client.schema import ParsingBlock, Poly, PrunedResult

# ブロックに割り当てられないスパンを末尾に送るための大きな順序値
_UNASSIGNED_ORDER = 10_000


def poly_to_bbox(poly: Poly) -> list[int]:
    """4点（またはn点）ポリゴンを軸平行 bbox [xmin,ymin,xmax,ymax] に変換する。"""
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    return [int(round(min(xs))), int(round(min(ys))), int(round(max(xs))), int(round(max(ys)))]


def overlap_area(a: list[int], b: list[int]) -> float:
    """2 つの軸平行 bbox の重なり面積。

    領域除外（orchestrator の region_mask）も同じ式で被覆率を出すため公開する。
    式が 2 箇所に分かれると、片方だけ丸め方を変えたときに「UI で見えている領域と
    実際に消える範囲がずれる」という気付きにくい差になる。
    """
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    w, h = max(0, ix2 - ix1), max(0, iy2 - iy1)
    return float(w * h)


_overlap_area = overlap_area  # 後方互換（本モジュール内の既存呼び出し）


def _block_order_for(bbox: list[int], blocks: list[ParsingBlock]) -> int:
    """bbox が最も重なる block の読み順（block_order→block_id→添字）を返す。"""
    best_order = _UNASSIGNED_ORDER
    best_overlap = 0.0
    for idx, block in enumerate(blocks):
        if not block.block_bbox:
            continue
        bb = [int(round(v)) for v in block.block_bbox[:4]]
        ov = _overlap_area(bbox, bb)
        if ov > best_overlap:
            best_overlap = ov
            order = block.block_order
            if order is None:
                order = block.block_id if block.block_id is not None else idx
            best_order = order
    return best_order


def build_spans(
    pruned: PrunedResult,
    *,
    page: int,
    start_id: int = 0,
    source: SpanSource = SpanSource.OCR,
) -> list[Span]:
    """overall_ocr_res から読み順ソート済みの Span 列を構築する。

    戻り値の span_id は start_id からの連番。呼び出し側は
    `start_id = 直前ページで返った最大 span_id + 1` を渡して跨ページ連番化する。
    """
    ocr = pruned.overall_ocr_res
    if ocr is None:
        return []

    raw: list[tuple[list[int], str, float, int]] = []
    for i, text in enumerate(ocr.rec_texts):
        score = ocr.rec_scores[i] if i < len(ocr.rec_scores) else 0.0
        if ocr.rec_boxes is not None and i < len(ocr.rec_boxes):
            # サービングは小数座標を返し得る。poly_to_bbox と同じ丸めに揃える
            bbox = [int(round(v)) for v in ocr.rec_boxes[i][:4]]
        elif i < len(ocr.rec_polys):
            bbox = poly_to_bbox(ocr.rec_polys[i])
        else:
            continue
        order = _block_order_for(bbox, pruned.parsing_res_list)
        raw.append((bbox, text, float(score), order))

    # (block順, top-y, left-x) で読み順ソート
    raw.sort(key=lambda r: (r[3], r[0][1], r[0][0]))

    spans: list[Span] = []
    for offset, (bbox, text, score, _order) in enumerate(raw):
        spans.append(
            Span(
                span_id=start_id + offset,
                page=page,
                text=text,
                conf=score,
                bbox=bbox,
                source=source,
            )
        )
    return spans


def build_layout_blocks(pruned: PrunedResult, *, page: int) -> list[LayoutBlock]:
    """parsing_res_list を LayoutBlock 列へ変換する（span_ids の対応付けは呼び出し側）。"""
    blocks: list[LayoutBlock] = []
    for idx, block in enumerate(pruned.parsing_res_list):
        bbox = [int(round(v)) for v in block.block_bbox[:4]] if block.block_bbox else [0, 0, 0, 0]
        blocks.append(
            LayoutBlock(
                page=page,
                label=block.block_label,
                bbox=bbox,
                content=block.block_content,
                block_order=block.block_order if block.block_order is not None else idx,
            )
        )
    return blocks
