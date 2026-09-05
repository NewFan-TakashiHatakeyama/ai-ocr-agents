"""除外領域と位置ガードの純関数（設計 docs/design/region-template-editor.md §5.1 / §5.5）。

ここで固定したいのは「どちら向きに失敗するか」である。除外は決定論的にデータを
消すので、判定が曖昧なときは**残す側**（かすった本文は消さない）に倒し、寸法が
分からないときは**適用しない側**（誤った位置を消さない）に倒す。
"""

from __future__ import annotations

from newfan_schemas import ExtractedField, Span, TableCell, TableResult, resolve_page

from newfan_orchestrator.region_mask import (
    filter_spans,
    mask_tables,
    region_mismatches,
    regions_for_page,
)

W, H = 1000, 1000
# 右上 1/4 を覆う領域（印影を想定）
STAMP = {"page": 1, "rect": [0.5, 0.0, 1.0, 0.5], "label": "社印"}


def _span(sid: int, bbox: list[int], page: int = 1) -> Span:
    return Span(span_id=sid, page=page, text="x", conf=0.9, bbox=bbox)


# ---------- resolve_page ----------


def test_resolve_page_last_and_null() -> None:
    assert resolve_page(None, 1, 3) and resolve_page(None, 3, 3)  # 全ページ
    assert resolve_page("last", 3, 3)
    assert not resolve_page("last", 2, 3)
    assert resolve_page(2, 2, 3) and not resolve_page(2, 1, 3)


def test_region_beyond_page_count_not_applied() -> None:
    """2 ページ帳票で作った p2 の領域は、1 ページ帳票には当てない。

    1 ページ目へ縮退させると、まったく違う位置を決定論削除することになる。
    """
    assert not resolve_page(2, 1, 1)
    assert regions_for_page([{"page": 2, "rect": [0.1, 0.1, 0.2, 0.2]}], 1, 1, W, H) == []


# ---------- regions_for_page ----------


def test_project_rounding() -> None:
    got = regions_for_page([STAMP], 1, 1, W, H)
    assert got == [[500, 0, 1000, 500]]


def test_regions_for_page_returns_empty_when_page_dims_missing() -> None:
    """寸法が無いページには適用しない（fail-open。§4.6）。

    pages.width/height は DDL で nullable。寸法不明のまま正規化座標を射影すると
    **誤った位置**を決定論削除することになるので、消し損ねる方を選ぶ。
    """
    for w, h in ((None, H), (W, None), (0, H), (W, 0), (None, None)):
        assert regions_for_page([STAMP], 1, 1, w, h) == []


# ---------- filter_spans ----------


def test_filter_spans_ratio_boundary() -> None:
    px = regions_for_page([STAMP], 1, 1, W, H)  # [500,0,1000,500]
    grazing = _span(1, [400, 0, 600, 100])   # 面積の 1/2 が領域内 = ちょうど 0.5
    inside = _span(2, [600, 100, 700, 200])  # 全没
    outside = _span(3, [0, 600, 100, 700])   # 完全に外
    barely = _span(4, [400, 0, 700, 100])    # 領域内は 2/3 → 除外

    kept, n = filter_spans([grazing, inside, outside, barely], px)
    # ちょうど 0.5 は「以上」なので除外される（境界の向きを固定する）
    assert [s.span_id for s in kept] == [3]
    assert n == 3


def test_filter_spans_keeps_grazing_text() -> None:
    """領域にかすっただけの本文は残す（安全側に倒す）。"""
    px = regions_for_page([STAMP], 1, 1, W, H)  # [500,0,1000,500]
    # 幅 400 のうち領域（x>=500）に入るのは 100 = 25%
    kept, n = filter_spans([_span(1, [200, 100, 600, 140])], px)
    assert n == 0 and len(kept) == 1


def test_filter_spans_zero_area_falls_back_to_center() -> None:
    """退化 bbox（面積 0）はゼロ除算になるので中心点包含で判定する。"""
    px = regions_for_page([STAMP], 1, 1, W, H)
    degenerate_in = _span(1, [700, 200, 700, 200])
    degenerate_out = _span(2, [100, 800, 100, 800])
    kept, n = filter_spans([degenerate_in, degenerate_out], px)
    assert [s.span_id for s in kept] == [2]
    assert n == 1


def test_filter_spans_noop_without_regions() -> None:
    spans = [_span(1, [0, 0, 10, 10])]
    kept, n = filter_spans(spans, [])
    assert kept == spans and n == 0


# ---------- mask_tables ----------


def _table(rows: list[dict[str, TableCell]], html: str | None = "<table/>") -> TableResult:
    return TableResult(name="t", page=1, structure_html=html, rows=rows)


def test_mask_tables_empties_cell_keeps_column_alignment() -> None:
    """セルは消さない。値と span 参照だけ落として bbox は残す。

    セルごと消すと検証画面の列が左詰めにずれ、別の項目の値に見えてしまう。
    """
    row = {
        "品名": TableCell(value="りんご", span_ids=[1], bbox=[100, 100, 200, 140]),
        "備考": TableCell(value="印影", span_ids=[2], bbox=[600, 100, 900, 140]),
    }
    px = regions_for_page([STAMP], 1, 1, W, H)
    out, stats = mask_tables([_table([row])], px)

    cells = out[0].rows[0]
    assert list(cells.keys()) == ["品名", "備考"]  # 列は保たれる
    assert cells["品名"].value == "りんご"
    assert cells["備考"].value is None and cells["備考"].span_ids == []
    assert cells["備考"].bbox == [600, 100, 900, 140]  # オーバーレイ表示用に残す
    assert stats.cells == 1 and stats.rows == 0


def test_mask_tables_drops_fully_emptied_row() -> None:
    row = {"a": TableCell(value="印", span_ids=[1], bbox=[600, 100, 700, 140])}
    out, stats = mask_tables([_table([row])], regions_for_page([STAMP], 1, 1, W, H))
    assert out[0].rows == []
    assert stats.cells == 1 and stats.rows == 1


def test_mask_tables_keeps_cell_with_corner_stamp() -> None:
    """セルの角に印影がかかった程度ではマスクしない（被覆 0.5 未満）。"""
    # セル面積 500x400 のうち領域に入るのは 200x400 = 40%
    row = {"a": TableCell(value="12,000", span_ids=[1], bbox=[200, 0, 700, 400])}
    out, stats = mask_tables([_table([row])], regions_for_page([STAMP], 1, 1, W, H))
    assert out[0].rows[0]["a"].value == "12,000"
    assert stats.cells == 0


def test_mask_tables_nullifies_structure_html_on_mask() -> None:
    """マスクした表の structure_html は落とす。

    pred_html は原文テキストをそのまま含むので、rows だけ拭いても HTML 経由で残る。
    """
    masked_row = {"a": TableCell(value="印", span_ids=[1], bbox=[600, 100, 900, 400])}
    plain_row = {"a": TableCell(value="ok", span_ids=[2], bbox=[10, 10, 90, 40])}
    px = regions_for_page([STAMP], 1, 1, W, H)
    out, _ = mask_tables([_table([masked_row]), _table([plain_row])], px)
    assert out[0].structure_html is None
    assert out[1].structure_html == "<table/>"  # 発動していない表は触らない


def test_mask_tables_noop_without_regions() -> None:
    row = {"a": TableCell(value="x", span_ids=[1], bbox=[600, 100, 900, 140])}
    out, stats = mask_tables([_table([row])], [])
    assert out[0].structure_html == "<table/>"
    assert not stats


# ---------- 位置ガード ----------

PAGES = [{"page_no": 1, "width": W, "height": H}]
SCHEMA = {
    "doc_type": "invoice",
    "fields": [{"name": "title", "type": "string", "region": {"page": 1, "rect": [0.3, 0.0, 0.7, 0.1]}}],
}


def _field(name: str, bbox: list[int], page: int = 1) -> ExtractedField:
    return ExtractedField(name=name, value_raw="x", page=page, bbox=bbox)


def test_guard_accepts_field_inside_tolerance() -> None:
    # region px = [300,0,700,100]。許容は各辺 max(1000*5%, 辺長*50%) = x:200 / y:50
    assert region_mismatches([_field("title", [320, 10, 680, 60])], SCHEMA, PAGES, 1) == []
    # 少しはみ出した程度は許容（スキャンのずれで毎回レビューにしない）
    assert region_mismatches([_field("title", [700, 60, 860, 130])], SCHEMA, PAGES, 1) == []


def test_guard_flags_far_field() -> None:
    assert region_mismatches([_field("title", [10, 800, 200, 860])], SCHEMA, PAGES, 1) == ["title"]


def test_guard_skips_field_without_bbox_or_region() -> None:
    no_bbox = ExtractedField(name="title", value_raw="x", page=1)
    assert region_mismatches([no_bbox], SCHEMA, PAGES, 1) == []
    assert region_mismatches([_field("other", [10, 800, 200, 860])], SCHEMA, PAGES, 1) == []


def test_guard_skips_when_page_dims_missing() -> None:
    """寸法が取れないページは判定しない（除外と同じ fail-open 規約）。"""
    pages = [{"page_no": 1, "image_uri": "x"}]
    assert region_mismatches([_field("title", [10, 800, 200, 860])], SCHEMA, pages, 1) == []


def test_guard_page_count_drift_ignores_page() -> None:
    """テンプレート化時と run のページ数が違うなら page 一致は問わない。

    ページ数可変帳票（明細が伸びる請求書等）で毎回 mismatch を記録すると、
    Phase 5 の許容パラメータ実測が汚染される。
    """
    pages2 = [
        {"page_no": 1, "width": W, "height": H},
        {"page_no": 2, "width": W, "height": H},
    ]
    # source_page_count=1 だが run は 2 ページ。p2 で座標は合っている
    assert region_mismatches([_field("title", [320, 10, 680, 60], page=2)], SCHEMA, pages2, 1) == []
    # 座標がずれていればページ数が違っても mismatch
    assert region_mismatches(
        [_field("title", [10, 800, 200, 860], page=2)], SCHEMA, pages2, 1
    ) == ["title"]


def test_guard_no_source_page_count_skips_page_judgement() -> None:
    """source_page_count が None（旧スキーマ）ならページ判定をしない。"""
    pages2 = [
        {"page_no": 1, "width": W, "height": H},
        {"page_no": 2, "width": W, "height": H},
    ]
    assert region_mismatches([_field("title", [320, 10, 680, 60], page=2)], SCHEMA, pages2, None) == []


def test_guard_flags_wrong_page_when_counts_match() -> None:
    """ページ数が一致していれば、別ページでの検出は mismatch。"""
    pages2 = [
        {"page_no": 1, "width": W, "height": H},
        {"page_no": 2, "width": W, "height": H},
    ]
    assert region_mismatches(
        [_field("title", [320, 10, 680, 60], page=2)], SCHEMA, pages2, 2
    ) == ["title"]
