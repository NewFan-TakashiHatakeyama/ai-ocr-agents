from newfan_schemas import (
    ExtractedField,
    FieldSchema,
    ReviewStatus,
    Span,
    SpanSource,
)


def test_span_defaults() -> None:
    span = Span(span_id=1, page=1, text="御請求金額", conf=0.97, bbox=[10, 20, 120, 46])
    assert span.source is SpanSource.OCR
    assert span.char_boxes is None


def test_extracted_field_defaults() -> None:
    field = ExtractedField(name="total_amount", value_raw="¥128,000")
    assert field.review_status is ReviewStatus.AUTO
    assert field.confidence == 0.0
    assert field.span_ids == []


def test_field_schema_critical_names() -> None:
    schema = FieldSchema.model_validate(
        {
            "doc_type": "invoice",
            "fields": [
                {"name": "total_amount", "type": "money_jpy", "critical": True},
                {"name": "note", "type": "string"},
            ],
        }
    )
    assert schema.critical_field_names() == {"total_amount"}
