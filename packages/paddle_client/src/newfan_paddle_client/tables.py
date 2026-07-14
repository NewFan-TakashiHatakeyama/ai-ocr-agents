"""構造由来テーブル抽出（§5.3）: PP-StructureV3 の table_res_list → TableResult。

pred_html（セル構造）を解析して行・列を復元し、cell_box_list（各セルの bbox）を
overall_ocr_res 由来の span と突き合わせて span_ids でグラウンディングする。
LLM の markdown 解釈より構造・座標が正確（テーブル主体帳票で有利）。

実測（sample.png / PP-StructureV3, enable_mkldnn=False）で判明した2つの実挙動に対応:
- B（span 値補完）: 表認識が空セルにしても、セル枠内に overall_ocr_res の span
  テキストがあれば値として復元する（例: 人数/箱数 の欠落を span から回復）。
- C（縦積みセル分割）: 「人数 箱数」のように1セルに縦積みされた列を、span の
  y 位置で複数列（人数 / 箱数）へ分割する。
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


def _spans_in_box(spans: list[Span], box: list[int]) -> list[Span]:
    """box 内に中心を持つ span を読み順（上→下, 左→右）で返す。"""
    x1, y1, x2, y2 = box
    inside: list[Span] = []
    for s in spans:
        b = s.bbox
        if not b or len(b) < 4:
            continue
        cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        if x1 <= cx <= x2 and y1 <= cy <= y2:
            inside.append(s)
    inside.sort(key=lambda s: ((s.bbox[1] + s.bbox[3]) / 2, s.bbox[0]))
    return inside


def _joined_text(spans: list[Span]) -> Optional[str]:
    """span 群の非空テキストを読み順に連結（B: 空セルの値復元）。全て空なら None。"""
    parts = [s.text for s in spans if s.text]
    return " ".join(parts) if parts else None


def _union_bbox(boxes: list[Optional[list[int]]]) -> Optional[list[int]]:
    valid = [b for b in boxes if b and len(b) >= 4]
    if not valid:
        return None
    return [
        min(b[0] for b in valid),
        min(b[1] for b in valid),
        max(b[2] for b in valid),
        max(b[3] for b in valid),
    ]


def _table_conf(tbl: TableRes) -> Optional[float]:
    top = tbl.table_ocr_pred
    if top is None or not top.rec_scores:
        return None
    return round(sum(top.rec_scores) / len(top.rec_scores), 4)


def _split_cell_vertically(
    cell: TableCell, labels: list[str], span_by_id: dict[int, Span]
) -> list[Optional[TableCell]]:
    """1セルを縦積みラベル数（k）に分割する（C）。

    優先度: セル内 span を y 昇順に k グループへ分配 → 各グループのテキストを割当。
    span が1個なら、セル中央より上/下で人数/箱数を判定。span 無しなら値を空白分割。
    """
    k = len(labels)
    result: list[Optional[TableCell]] = [None] * k
    spans = [span_by_id[i] for i in cell.span_ids if i in span_by_id and span_by_id[i].text]
    spans.sort(key=lambda s: (s.bbox[1] + s.bbox[3]) / 2)

    if len(spans) >= k:
        groups: list[list[Span]] = [[] for _ in range(k)]
        n = len(spans)
        for idx, s in enumerate(spans):
            groups[min(k - 1, idx * k // n)].append(s)
        for gi, grp in enumerate(groups):
            if grp:
                result[gi] = TableCell(
                    value=(" ".join(s.text for s in grp if s.text) or None),
                    span_ids=[s.span_id for s in grp],
                    bbox=_union_bbox([s.bbox for s in grp]),
                )
        return result

    if len(spans) == 1:
        s = spans[0]
        parts = (s.text or "").split()
        if len(parts) >= k:  # 1 span に "20 10" のように k 値が入るケース
            for i in range(k):
                result[i] = TableCell(value=parts[i], span_ids=[s.span_id], bbox=s.bbox)
        else:
            cy = (s.bbox[1] + s.bbox[3]) / 2
            mid = (cell.bbox[1] + cell.bbox[3]) / 2 if cell.bbox else cy
            gi = 0 if cy <= mid else min(1, k - 1)
            result[gi] = TableCell(value=s.text or None, span_ids=[s.span_id], bbox=s.bbox)
        return result

    if cell.value:  # span 無し・値のみ → 空白分割
        parts = cell.value.split()
        for i in range(min(k, len(parts))):
            result[i] = TableCell(value=parts[i], span_ids=list(cell.span_ids), bbox=cell.bbox)
    return result


def _split_stacked_columns(
    col_names: list[str],
    rows: list[dict[str, TableCell]],
    span_by_id: dict[int, Span],
) -> tuple[list[str], list[dict[str, TableCell]]]:
    """ヘッダが空白区切りの複数ラベル（例「人数 箱数」）で、実際にセルが縦積み
    （複数 span が上下）になっている列を検出し、span の y 位置で分割する（C）。

    誤検出防止: ヘッダが k(>=2) 語かつ、いずれかの行のセルが k 個以上の span、
    または k 個以上の空白区切り値を持つ列のみを対象にする。
    """
    stacked: dict[str, list[str]] = {}
    for name in col_names:
        tokens = name.split()
        if len(tokens) < 2:
            continue
        k = len(tokens)
        for row in rows:
            c = row.get(name)
            if c is None:
                continue
            if len(c.span_ids) >= k or (c.value and len(c.value.split()) >= k):
                stacked[name] = tokens
                break
    if not stacked:
        return col_names, rows

    new_cols: list[str] = []
    for name in col_names:
        new_cols.extend(stacked.get(name, [name]))

    new_rows: list[dict[str, TableCell]] = []
    for row in rows:
        nr: dict[str, TableCell] = {}
        for name, cell in row.items():
            if name not in stacked:
                nr[name] = cell
                continue
            labels = stacked[name]
            for lbl, sub in zip(labels, _split_cell_vertically(cell, labels, span_by_id)):
                if sub is not None:
                    nr[lbl] = sub
        new_rows.append(nr)
    return new_cols, new_rows


def build_tables(pruned: PrunedResult, spans: list[Span], *, page: int) -> list[TableResult]:
    """table_res_list を TableResult 列に変換する（空行は除去、セルを span でグラウンディング）。"""
    span_by_id = {s.span_id: s for s in spans}
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
                inside = _spans_in_box(spans, box) if box else []
                # B: 表認識が空にしたセルでも、枠内 span のテキストから値を復元
                value = text or _joined_text(inside)
                sids = [s.span_id for s in inside]
                if value or sids:
                    name = col_names[col] if col < len(col_names) else f"col{col + 1}"
                    row_dict[name] = TableCell(value=value, span_ids=sids, bbox=box)
                col += colspan
            # 実データ行のみ残す（値が1つも無い＝余白行/空 span だけの行は除去）
            if any(c.value for c in row_dict.values()):
                rows_out.append(row_dict)

        # C: 縦積み列（人数 箱数 等）を span の y 位置で分割
        _col_names_final, rows_out = _split_stacked_columns(col_names, rows_out, span_by_id)

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
