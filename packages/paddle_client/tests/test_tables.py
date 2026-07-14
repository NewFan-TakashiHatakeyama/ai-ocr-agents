"""構造由来テーブル抽出 build_tables の契約テスト（実 PP-StructureV3 fixture）。"""

from __future__ import annotations

import json
from pathlib import Path

from newfan_paddle_client import LayoutParsingResponse, build_spans, build_tables

_FIXTURE = Path(__file__).parent / "fixtures" / "real_layout_parsing_sample.json"


def _pruned():
    data = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    return LayoutParsingResponse.model_validate(data).layout_parsing_results[0].pruned_result


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


def _rows_by_product(substr: str):
    pr = _pruned()
    spans = build_spans(pr, page=1)
    rows = build_tables(pr, spans, page=1)[0].rows
    return next(
        r for r in rows if r.get("商品名") and r["商品名"].value and substr in r["商品名"].value
    )


def test_span_backfill_recovers_empty_html_cell() -> None:
    """B: 表認識が空にしたセルでも、枠内 span から値を復元する。

    実 fixture の「冷凍ピザ」行は人数/箱数セルが pred_html 上は空だが、
    overall_ocr_res の span が存在するため値が復元される（None にならない）。
    """
    piza = _rows_by_product("ピザ")
    assert piza["人数"].value, "空セルが span テキストから復元される"
    assert piza["人数"].span_ids, "復元値は span にグラウンディングされる"


def test_stacked_column_is_split() -> None:
    """C: 縦積みの「人数 箱数」列が人数/箱数の2列に分割される。"""
    pr = _pruned()
    spans = build_spans(pr, page=1)
    rows = build_tables(pr, spans, page=1)[0].rows
    cols = {k for row in rows for k in row}
    assert "人数" in cols and "箱数" in cols
    assert "人数 箱数" not in cols  # 元の結合列は残らない

    # 「カッ味噌味」行は縦積み値 20/10 が人数=20・箱数=10 に分かれる
    miso = _rows_by_product("味噌")
    assert miso["人数"].value == "20" and miso["箱数"].value == "10"


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
