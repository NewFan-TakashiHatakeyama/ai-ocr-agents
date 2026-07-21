"""構造由来テーブル抽出 build_tables のテスト。

B（span 値補完）/C（縦積み分割）のロジックは**合成 fixture で決定論的に**検証する
（実 OCR の読み値に依存させると、モデル更改 PP-OCRv5→v6 のたびに壊れて意味を失うため）。
実 PP-StructureV3 fixture は「構造契約」（列分割・合計行・grounding）の検証に使う。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from newfan_paddle_client import LayoutParsingResponse, build_spans, build_tables
from newfan_paddle_client.schema import PrunedResult

_FIXTURE = Path(__file__).parent / "fixtures" / "real_layout_parsing_sample.json"


def _pruned():
    data = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    return LayoutParsingResponse.model_validate(data).layout_parsing_results[0].pruned_result


def _poly(x1: int, y1: int, x2: int, y2: int) -> list[list[int]]:
    return [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]


def _synthetic(
    pred_html: str, cell_boxes: list[list[int]], texts: list[str], polys: list[list[list[int]]]
) -> PrunedResult:
    data: dict[str, Any] = {
        "parsing_res_list": [],
        "overall_ocr_res": {
            "rec_texts": texts,
            "rec_scores": [0.9] * len(texts),
            "rec_polys": polys,
        },
        "table_res_list": [
            {
                "pred_html": pred_html,
                "cell_box_list": cell_boxes,
                "table_ocr_pred": {"rec_texts": texts, "rec_scores": [0.9] * len(texts)},
            }
        ],
    }
    return PrunedResult.model_validate(data)


def test_build_tables_from_real_fixture() -> None:
    pr = _pruned()
    spans = build_spans(pr, page=1)
    tables = build_tables(pr, spans, page=1)

    assert len(tables) == 1
    t = tables[0]
    assert t.page == 1
    assert t.structure_html and "<table>" in t.structure_html  # 構造保持
    assert t.confidence is not None and 0.0 <= t.confidence <= 1.0
    # 空行（見積フォームの余白 22 行）は除去され、実データ行のみ残る
    assert 1 <= len(t.rows) < 20

    # 明細の値が抽出される
    goods = [c.value for row in t.rows for k, c in row.items() if k.startswith("商品名")]
    assert any(v and "コロッケ" in v for v in goods)

    # 合計行の 136,998 が含まれる
    all_vals = [c.value for row in t.rows for c in row.values() if c.value]
    assert any("136,998" in v for v in all_vals)


def test_table_cells_grounded_to_spans() -> None:
    pr = _pruned()
    spans = build_spans(pr, page=1)
    tables = build_tables(pr, spans, page=1)
    valid_ids = {s.span_id for s in spans}
    grounded = [
        sid for row in tables[0].rows for c in row.values() for sid in c.span_ids
    ]
    assert grounded, "セルが overall_ocr_res の span にグラウンディングされる"
    assert all(sid in valid_ids for sid in grounded)


def test_span_backfill_recovers_empty_html_cell() -> None:
    """B: 表認識が空セルにしても、枠内 span のテキストから値を復元する（合成・決定論）。"""
    pr = _synthetic(
        # 数量セルは pred_html 上は空。だが枠内に span "3" がある。
        "<html><body><table><tbody>"
        "<tr><td>品名</td><td>数量</td></tr>"
        "<tr><td>りんご</td><td></td></tr>"
        "</tbody></table></body></html>",
        [[10, 10, 50, 30], [50, 10, 90, 30], [10, 40, 50, 60], [50, 40, 90, 60]],
        ["品名", "数量", "りんご", "3"],
        [_poly(20, 15, 40, 25), _poly(60, 15, 80, 25), _poly(20, 45, 40, 55), _poly(60, 45, 80, 55)],
    )
    rows = build_tables(pr, build_spans(pr, page=1), page=1)[0].rows
    assert len(rows) == 1
    assert rows[0]["数量"].value == "3", "空セルが span テキストから復元される"
    assert rows[0]["数量"].span_ids, "復元値は span にグラウンディングされる"


def test_stacked_column_split_by_span_y() -> None:
    """C: 縦積み「人数 箱数」列が span の y 位置で人数/箱数へ分割される（合成・決定論）。"""
    pr = _synthetic(
        "<html><body><table><tbody>"
        "<tr><td>品名</td><td>人数 箱数</td></tr>"
        "<tr><td>りんご</td><td>25 10</td></tr>"
        "</tbody></table></body></html>",
        [[10, 10, 50, 30], [50, 10, 90, 30], [10, 40, 50, 80], [50, 40, 90, 80]],
        ["品名", "人数 箱数", "りんご", "25", "10"],
        [
            _poly(20, 15, 40, 25),
            _poly(60, 15, 80, 25),
            _poly(20, 50, 40, 70),
            _poly(60, 45, 80, 58),  # 上段 → 人数
            _poly(60, 62, 80, 75),  # 下段 → 箱数
        ],
    )
    rows = build_tables(pr, build_spans(pr, page=1), page=1)[0].rows
    cols = {k for row in rows for k in row}
    assert "人数" in cols and "箱数" in cols
    assert "人数 箱数" not in cols, "元の結合列は残らない"

    row = rows[0]
    assert row["人数"].value == "25" and row["箱数"].value == "10"
    # 上段/下段がそれぞれ実 span の bbox を保持する（セル↔画像 grounding のため）
    assert row["人数"].bbox[1] < row["箱数"].bbox[1]
    assert row["人数"].span_ids != row["箱数"].span_ids


def test_stacked_column_split_on_real_fixture() -> None:
    """実 fixture でも縦積み列が構造として分割される（値は OCR 依存なので見ない）。"""
    pr = _pruned()
    rows = build_tables(pr, build_spans(pr, page=1), page=1)[0].rows
    cols = {k for row in rows for k in row}
    assert "人数" in cols and "箱数" in cols
    assert "人数 箱数" not in cols


def test_real_fixture_captures_totals() -> None:
    """PP-OCRv6 実測: 合計(税抜)126,850 / 消費税(外税)10,148 / 合計136,998 が行として残る。"""
    pr = _pruned()
    rows = build_tables(pr, build_spans(pr, page=1), page=1)[0].rows
    vals = {c.value for row in rows for c in row.values() if c.value}
    for expected in ("136,998", "126,850", "10,148"):
        assert any(expected in v for v in vals), f"{expected} が抽出される"


def test_empty_padding_row_is_dropped() -> None:
    """値が1つも無い余白行（span が空テキストのみ）は除去される。"""
    pr = _pruned()
    spans = build_spans(pr, page=1)
    rows = build_tables(pr, spans, page=1)[0].rows
    for row in rows:
        assert any(c.value for c in row.values()), "全セル空の行は残らない"


def test_empty_table_res_yields_no_tables() -> None:
    from newfan_paddle_client.schema import PrunedResult

    assert build_tables(PrunedResult(), [], page=1) == []
