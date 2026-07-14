from newfan_schemas import ExtractedField, TableCell, TableResult

from newfan_export import (
    CsvColumn,
    CsvMapping,
    ExportInput,
    build_canonical_json,
    to_line_items_csv,
    to_main_csv,
)


def _input() -> ExportInput:
    return ExportInput(
        tenant_id="ten_1",
        document_id="doc_1",
        run_id="run_1",
        engine_versions={"paddleocr": "3.7.0"},
        fields=[
            ExtractedField(name="total_amount", value_normalized="128000", confidence=0.98),
            ExtractedField(name="invoice_date", value_normalized="2026-05-01", confidence=0.98),
        ],
        tables=[
            TableResult(
                name="line_items",
                rows=[
                    {"item": TableCell(value="コンサル費"), "amount": TableCell(value="128000")},
                    {"item": TableCell(value="送料"), "amount": TableCell(value="0")},
                ],
            )
        ],
        review_summary={"pending": 0, "auto": 2},
    )


def test_canonical_json_shape() -> None:
    doc = build_canonical_json(_input())
    assert doc["document_id"] == "doc_1"
    assert doc["engine_versions"]["paddleocr"] == "3.7.0"
    total = next(f for f in doc["fields"] if f["name"] == "total_amount")
    assert total["final"] == "128000"


def test_main_csv_default_columns() -> None:
    csv_text = to_main_csv(_input())
    lines = csv_text.strip().splitlines()
    assert lines[0] == "document_id,total_amount,invoice_date"
    assert lines[1] == "doc_1,128000,2026-05-01"


def test_main_csv_mapping() -> None:
    mapping = CsvMapping(columns=[CsvColumn("金額", "total_amount"), CsvColumn("日付", "invoice_date")])
    csv_text = to_main_csv(_input(), mapping)
    assert csv_text.splitlines()[0] == "document_id,金額,日付"


def test_line_items_csv() -> None:
    csv_text = to_line_items_csv(_input())
    lines = csv_text.strip().splitlines()
    assert lines[0] == "document_id,item,amount"
    assert lines[1] == "doc_1,コンサル費,128000"
    assert lines[2] == "doc_1,送料,0"


def test_line_items_csv_empty_when_absent() -> None:
    inp = _input()
    inp.tables = []
    assert to_line_items_csv(inp) == ""
