"""除外領域の適用と読取領域の位置ガード（設計 docs/design/region-template-editor.md §5）。

純関数のみ。langgraph にも DB にも依存しない。

設計上の要点:

- **除外は決定論的にデータを消す**。だからこそ「消したこと」を必ず数えて返す。
  件数が run metrics → 検証画面のバッジまで届くのが、査閲者が「画像にはあるのに
  結果に無い」理由を知る唯一の経路である（ReviewItem は永続化されない）。
- **寸法が分からないページでは何もしない（fail-open）**。``pages.width/height`` は
  DDL で nullable であり、寸法不明のまま正規化座標を射影すると**誤った位置**を
  決定論削除することになる。消し損ねより誤削除の方が危険なので、適用せず記録に残す。
- **読取領域は値を捨てる根拠にしない**。位置ガードは「設定と違う場所で見つかった」を
  観測するだけで、confidence にも値にも触らない。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional

from newfan_paddle_client.spans import overlap_area
from newfan_schemas import ExtractedField, Span, TableCell, TableResult, resolve_page

# span / セルの面積のうち領域に覆われた割合がこれ以上なら除外する。
# 「交差したら即除外」は本文を巻き込み、中心点だけを見る方式は境界を跨ぐ span で
# 不安定になる。0.5 は失敗の向きが安全側（かすった本文は残り、印影自身の OCR
# ゴミは全没して確実に落ちる）。UI 側は「対象より少し大きめに描く」運用で補う。
EXCLUDE_SPAN_RATIO = 0.5
EXCLUDE_CELL_RATIO = 0.5

# --- 位置ガードの許容パラメータ（shadow 実測で確定してから enforce する。§5.5 / §11-1）---
# 各辺方向の許容幅 = max(ページ寸法 * PAGE, 領域の当該辺長 * SIDE)
REGION_GUARD_PAD_PAGE_RATIO = 0.05
REGION_GUARD_PAD_SIDE_RATIO = 0.50
# doc レベル判定（「別レイアウトの帳票」とみなして per-field レビューを抑止する）を
# 適用する最小 region 件数。n=1 なら 1 件の mismatch が常に「過半」になり、抑止が
# 常に効いてガードが一度もレビューを出さない。n=2 も過半の判別が成立しない。
REGION_GUARD_MIN_FIELDS_FOR_LAYOUT = 3


def guard_enforced() -> bool:
    """位置ガードを実際にレビューへ反映するか（既定 off = shadow mode）。

    v1 は metrics とログだけを書き、confidence も ReviewItem も触らない。誤 mismatch
    率を実測してから有効化する（設計 D10 / Phase 5）。
    """
    return os.environ.get("REGION_GUARD_ENFORCE", "").lower() in ("1", "true", "yes", "on")


BBox = list[int]


@dataclass
class MaskStats:
    """テーブルマスクの結果。0 件でないなら run を needs_review へ倒す根拠になる。"""

    cells: int = 0
    rows: int = 0

    def __bool__(self) -> bool:
        return bool(self.cells or self.rows)


@dataclass
class RegionStats:
    """``metrics["region"]`` に載せる観測値（設計 §5.4）。"""

    excluded_spans: int = 0
    excluded_cells: int = 0
    excluded_rows: int = 0
    skipped_pages_no_dims: list[int] = field(default_factory=list)
    markdown_dropped_pages: list[int] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "excluded_spans": self.excluded_spans,
            "excluded_cells": self.excluded_cells,
            "excluded_rows": self.excluded_rows,
            "skipped_pages_no_dims": sorted(set(self.skipped_pages_no_dims)),
            "markdown_dropped_pages": sorted(set(self.markdown_dropped_pages)),
        }


def _rect_of(region: Any) -> Optional[list[float]]:
    rect = region.get("rect") if isinstance(region, dict) else getattr(region, "rect", None)
    if not rect or len(rect) < 4:
        return None
    return [float(v) for v in rect[:4]]


def _page_of(region: Any) -> Any:
    return region.get("page") if isinstance(region, dict) else getattr(region, "page", None)


def project(rect: list[float], width: int, height: int) -> BBox:
    """正規化 rect [0,1] を当該ページの画素矩形へ射影する（DD-01 の座標系）。"""
    return [
        int(round(rect[0] * width)),
        int(round(rect[1] * height)),
        int(round(rect[2] * width)),
        int(round(rect[3] * height)),
    ]


def regions_for_page(
    exclude_regions: list[Any],
    page_no: int,
    page_count: int,
    page_w: Optional[int],
    page_h: Optional[int],
) -> list[BBox]:
    """このページに適用される除外領域を画素矩形で返す。

    ``page_w`` / ``page_h`` が None または 0 以下なら **[] を返す**（fail-open）。
    呼び出し側は ``skipped_pages_no_dims`` に page_no を積んで無音化を防ぐこと。
    """
    if not exclude_regions:
        return []
    if not page_w or not page_h or page_w <= 0 or page_h <= 0:
        return []
    out: list[BBox] = []
    for r in exclude_regions:
        rect = _rect_of(r)
        if rect is None:
            continue
        if not resolve_page(_page_of(r), page_no, page_count):
            continue
        out.append(project(rect, int(page_w), int(page_h)))
    return out


def _covered(bbox: Optional[list[int]], px_regions: list[BBox], ratio: float) -> bool:
    if not bbox or len(bbox) < 4 or not px_regions:
        return False
    area = float((bbox[2] - bbox[0]) * (bbox[3] - bbox[1]))
    if area <= 0:
        # 退化 bbox（poly_to_bbox が 1 行の細い矩形を潰した場合など）はゼロ除算に
        # なるので中心点包含へフォールバックする。判定不能にして素通しさせない。
        cx, cy = (bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0
        return any(r[0] <= cx <= r[2] and r[1] <= cy <= r[3] for r in px_regions)
    return any(overlap_area(bbox, r) / area >= ratio for r in px_regions)


def filter_spans(spans: list[Span], px_regions: list[BBox]) -> tuple[list[Span], int]:
    """除外領域に覆われた span を落とす。戻り値は (残った span, 除外件数)。

    **DD-02 の再 OCR より前**に呼ぶこと。後に置くと印影のゴミ文字へ crop 再認識の
    課金が発生する。
    """
    if not px_regions:
        return list(spans), 0
    kept: list[Span] = []
    excluded = 0
    for s in spans:
        if _covered(s.bbox, px_regions, EXCLUDE_SPAN_RATIO):
            excluded += 1
        else:
            kept.append(s)
    return kept, excluded


def _cell_is_empty(cell: TableCell) -> bool:
    return not (cell.value or "").strip() and not cell.span_ids


def mask_tables(
    tables: list[TableResult], px_regions: list[BBox]
) -> tuple[list[TableResult], MaskStats]:
    """除外領域に重なるセルの値を空にする。**セル自体は削除しない**。

    セルを消すと検証画面の列が左詰めにずれて別の項目に見えてしまうため、値と
    span 参照だけを落とし bbox は残す（オーバーレイで「ここは除外設定です」と
    示せるようにする）。全セルが空になった行だけは行ごと落とす——これは
    build_tables が元から空行を落としているのと同じ規則なので列ズレは起きない。

    1 セルでもマスクした表は ``structure_html`` を None にする。pred_html は
    原文テキストをそのまま含むため、rows だけ拭いても HTML 経由で残ってしまう。
    """
    stats = MaskStats()
    if not px_regions:
        return list(tables), stats

    out: list[TableResult] = []
    for t in tables:
        masked_any = False
        new_rows: list[dict[str, TableCell]] = []
        for row in t.rows:
            row_masked = False
            new_row: dict[str, TableCell] = {}
            for col, cell in row.items():
                if _covered(cell.bbox, px_regions, EXCLUDE_CELL_RATIO) and not _cell_is_empty(cell):
                    new_row[col] = TableCell(value=None, span_ids=[], bbox=cell.bbox)
                    stats.cells += 1
                    row_masked = True
                    masked_any = True
                else:
                    new_row[col] = cell
            # マスクした結果として全セルが空になった行だけを落とす（元から空の行は
            # build_tables が既に除いているので、ここで数えると二重計上になる）
            if row_masked and all(_cell_is_empty(c) for c in new_row.values()):
                stats.rows += 1
                continue
            new_rows.append(new_row)
        out.append(
            TableResult(
                name=t.name,
                page=t.page,
                structure_html=None if masked_any else t.structure_html,
                rows=new_rows,
                confidence=t.confidence,
            )
        )
    return out, stats


# ---------------- include 領域の位置ガード（§5.5） ----------------


def _dims_by_page(pages: list[dict[str, Any]]) -> dict[int, tuple[Optional[int], Optional[int]]]:
    out: dict[int, tuple[Optional[int], Optional[int]]] = {}
    for p in pages:
        try:
            no = int(p["page_no"])
        except (KeyError, TypeError, ValueError):
            continue
        out[no] = (p.get("width"), p.get("height"))
    return out


def regions_by_field(schema: dict[str, Any]) -> dict[str, Any]:
    """スキーマの fields から ``name -> region`` を引く（region 付きのみ）。"""
    out: dict[str, Any] = {}
    for f in schema.get("fields") or []:
        if not isinstance(f, dict):
            continue
        region = f.get("region")
        name = f.get("name")
        if region and name:
            out[str(name)] = region
    return out


def region_mismatches(
    fields: list[ExtractedField],
    schema: dict[str, Any],
    pages: list[dict[str, Any]],
    source_page_count: Optional[int],
) -> list[str]:
    """読取領域と実際の検出位置がずれた field 名を返す（値は一切変えない）。

    判定は field bbox の**中心点**が、領域を各辺方向へ
    ``max(ページ寸法 * 5%, 領域の当該辺長 * 50%)`` 広げた矩形に入るか。中心点に
    するのは、スキャンのずれで矩形が少し外へはみ出しただけの正常ケースを
    mismatch にしないため。

    判定しない（mismatch に数えない）ケース:
    - region が無い / field に bbox が無い（F-0 前の run や根拠なし項目）
    - ``field.page`` の寸法が取れない（fail-open。§4.6）
    - ``source_page_count`` が None（旧スキーマ等）→ ページ判定を行わない
    - ``source_page_count`` と run のページ数が違い、region.page が int →
      ページ判定を行わない（ページ数可変帳票で毎回 mismatch になるのを避ける）
    """
    by_name = regions_by_field(schema)
    if not by_name:
        return []
    dims = _dims_by_page(pages)
    page_count = len(pages)
    page_count_drift = source_page_count is not None and source_page_count != page_count

    out: list[str] = []
    for f in fields:
        region = by_name.get(f.name)
        if region is None or not f.bbox or len(f.bbox) < 4 or f.page is None:
            continue
        rect = _rect_of(region)
        if rect is None:
            continue
        w, h = dims.get(int(f.page), (None, None))
        if not w or not h or w <= 0 or h <= 0:
            continue  # 寸法不明のページは判定しない

        page_spec = _page_of(region)
        check_page = source_page_count is not None and not (
            page_count_drift and isinstance(page_spec, int)
        )
        if check_page and not resolve_page(page_spec, int(f.page), page_count):
            out.append(f.name)
            continue

        px = project(rect, int(w), int(h))
        pad_x = max(w * REGION_GUARD_PAD_PAGE_RATIO, (px[2] - px[0]) * REGION_GUARD_PAD_SIDE_RATIO)
        pad_y = max(h * REGION_GUARD_PAD_PAGE_RATIO, (px[3] - px[1]) * REGION_GUARD_PAD_SIDE_RATIO)
        cx = (f.bbox[0] + f.bbox[2]) / 2.0
        cy = (f.bbox[1] + f.bbox[3]) / 2.0
        if not (px[0] - pad_x <= cx <= px[2] + pad_x and px[1] - pad_y <= cy <= px[3] + pad_y):
            out.append(f.name)
    return out
