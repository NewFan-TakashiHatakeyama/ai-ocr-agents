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


def test_empty_table_res_yields_no_tables() -> None:
    from newfan_paddle_client.schema import PrunedResult

    assert build_tables(PrunedResult(), [], page=1) == []
