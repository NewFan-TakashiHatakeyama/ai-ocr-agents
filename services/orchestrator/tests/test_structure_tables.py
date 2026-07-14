"""structure_ocr が構造由来テーブルを state.tables に載せ、kie が保持することを検証。"""

from __future__ import annotations

from typing import Any

from newfan_paddle_client import LayoutParsingResponse

from newfan_orchestrator.ocr_nodes import make_structure_ocr

# 2列×1行の最小テーブル。overall_ocr_res の poly をセル box 内に置きグラウンディングさせる。
_LAYOUT: dict[str, Any] = {
    "layoutParsingResults": [
        {
            "prunedResult": {
                "parsing_res_list": [
                    {"block_bbox": [10, 10, 90, 60], "block_label": "table", "block_content": "", "block_id": 0, "block_order": 0}
                ],
                "overall_ocr_res": {
                    "rec_texts": ["品名", "数量", "りんご", "3"],
                    "rec_scores": [0.99, 0.98, 0.95, 0.9],
                    "rec_polys": [
                        [[20, 15], [40, 15], [40, 25], [20, 25]],
                        [[60, 15], [80, 15], [80, 25], [60, 25]],
                        [[20, 45], [40, 45], [40, 55], [20, 55]],
                        [[60, 45], [80, 45], [80, 55], [60, 55]],
                    ],
                },
                "table_res_list": [
                    {
                        "pred_html": "<html><body><table><tbody><tr><td>品名</td><td>数量</td></tr><tr><td>りんご</td><td>3</td></tr></tbody></table></body></html>",
                        "cell_box_list": [
                            [10, 10, 50, 30], [50, 10, 90, 30],
                            [10, 40, 50, 60], [50, 40, 90, 60],
                        ],
                        "table_ocr_pred": {"rec_texts": ["品名", "数量", "りんご", "3"], "rec_scores": [0.99, 0.98, 0.95, 0.9]},
                    }
                ],
            },
            "markdown": {"text": "| 品名 | 数量 |\n| りんご | 3 |"},
        }
    ]
}


class _FakeStructure:
    def layout_parsing(self, file_b64: str, *, file_type: int = 1) -> LayoutParsingResponse:
        return LayoutParsingResponse.model_validate(_LAYOUT)


def test_structure_ocr_emits_grounded_table() -> None:
    node = make_structure_ocr(_FakeStructure(), image_loader=lambda uri: b"x")
    out = node({"pages": [{"page_no": 1, "image_uri": "x"}]})

    assert "tables" in out and len(out["tables"]) == 1
    t = out["tables"][0]
    assert t.page == 1 and len(t.rows) == 1
    row = t.rows[0]
    assert row["品名"].value == "りんご" and row["数量"].value == "3"
    # セルが overall_ocr_res の span にグラウンディングされている
    valid = {s.span_id for s in out["spans"]}
    assert row["品名"].span_ids and all(i in valid for i in row["品名"].span_ids)
