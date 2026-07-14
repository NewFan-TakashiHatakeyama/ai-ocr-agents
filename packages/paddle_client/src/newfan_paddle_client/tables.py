"""構造由来テーブル抽出（§5.3）: PP-StructureV3 の table_res_list → TableResult。

pred_html（セル構造）を解析して行・列を復元し、cell_box_list（各セルの bbox）を
overall_ocr_res 由来の span と突き合わせて span_ids でグラウンディングする。
LLM の markdown 解釈より構造・座標が正確（テーブル主体帳票で有利）。
"""

from __future__ import annotations

import re
from typing import Optional

from newfan_schemas import Span, TableCell, TableResult

from newfan_paddle_client.schema import PrunedResult, TableRes

_TR = re.compile(r"<tr>(.*?)</tr>", re.S)
_TD = re.compile(r'<td(?:\s+colspan="(\d+)")?(?:\s+rowspan="\d+")?>(.*?)</td>', re.S)
_TAG = re.compile(r"<[^>]+>")


def _parse_rows(html: str) -> list[list[tuple[str, int]]]:
    """pred_html を [行][(セルテキスト, colspan)] に分解する。"""
    rows: list[list[tuple[str, int]]] = []
    for tr in _TR.findall(html or ""):
        cells: list[tuple[str, int]] = []
        for m in _TD.finditer(tr):
            colspan = int(m.group(1) or 1)
            text = _TAG.sub("", m.group(2)).strip()
            cells.append((text, colspan))
        if cells:
            rows.append(cells)
    return rows


def _column_names(header: list[tuple[str, int]]) -> list[str]:
    names: list[str] = []
    for i, (text, colspan) in enumerate(header):
        base = text or f"col{i + 1}"
        for j in range(colspan):
            names.append(base if j == 0 else f"{base}_{j + 1}")
    return names


def _to_bbox(raw: object) -> Optional[list[int]]:
    if not isinstance(raw, (list, tuple)) or len(raw) < 4:
        return None
    return [int(round(float(raw[i]))) for i in range(4)]


def _spans_in_box(spans: list[Span], box: list[int]) -> list[int]:
    x1, y1, x2, y2 = box
    out: list[int] = []
    for s in spans:
        b = s.bbox
        if not b or len(b) < 4:
            continue
        cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        if x1 <= cx <= x2 and y1 <= cy <= y2:
            out.append(s.span_id)
    return out


def _table_conf(tbl: TableRes) -> Optional[float]:
    top = tbl.table_ocr_pred
    if top is None or not top.rec_scores:
        return None
    return round(sum(top.rec_scores) / len(top.rec_scores), 4)


def build_tables(pruned: PrunedResult, spans: list[Span], *, page: int) -> list[TableResult]:
    """table_res_list を TableResult 列に変換する（空行は除去、セルを span でグラウンディング）。"""
    results: list[TableResult] = []
    for tbl in pruned.table_res_list:
        grid = _parse_rows(tbl.pred_html or "")
        if not grid:
            continue
        boxes = [_to_bbox(b) for b in (tbl.cell_box_list or [])]

        # HTML セルを読み順で box にマッピング
        cell_box: list[list[Optional[list[int]]]] = []
        gi = 0
        for row in grid:
            rb: list[Optional[list[int]]] = []
            for _cell in row:
                rb.append(boxes[gi] if gi < len(boxes) else None)
                gi += 1
            cell_box.append(rb)

        col_names = _column_names(grid[0])
        rows_out: list[dict[str, TableCell]] = []
        for r in range(1, len(grid)):
            row = grid[r]
            row_dict: dict[str, TableCell] = {}
            col = 0
            for j, (text, colspan) in enumerate(row):
                box = cell_box[r][j]
                sids = _spans_in_box(spans, box) if box else []
                if text or sids:
                    name = col_names[col] if col < len(col_names) else f"col{col + 1}"
                    row_dict[name] = TableCell(value=text or None, span_ids=sids, bbox=box)
                col += colspan
            if row_dict:  # 空行（見積フォームの余白行）は除去
                rows_out.append(row_dict)

        results.append(
            TableResult(
                name="table",
                page=page,
                structure_html=tbl.pred_html,
                rows=rows_out,
                confidence=_table_conf(tbl),
            )
        )
    return results
