from newfan_schemas import ExtractedField, FieldSchema

from newfan_orchestrator.gate import Thresholds, confidence_gate, threshold_for


def _schema() -> FieldSchema:
    return FieldSchema.model_validate(
        {
            "doc_type": "invoice",
            "fields": [
                {"name": "total_amount", "type": "money_jpy", "critical": True},
                {"name": "note", "type": "string"},
            ],
        }
    )


def test_threshold_for() -> None:
    t = Thresholds()
    assert threshold_for(True, t) == 0.90
    assert threshold_for(False, t) == 0.80


def test_critical_below_threshold_needs_review() -> None:
    fields = [
        ExtractedField(name="total_amount", confidence=0.85, grounding_score=1.0),
        ExtractedField(name="note", confidence=0.85, grounding_score=1.0),
    ]
    items = confidence_gate(fields, _schema())
    # total_amount(critical, 0.85<0.90) は要レビュー、note(0.85>=0.80) は自動
    names = {i.field_name for i in items}
    assert names == {"total_amount"}
    assert items[0].critical is True


def test_no_grounding_forces_review() -> None:
    fields = [ExtractedField(name="note", confidence=0.99, grounding_score=0.0)]
    items = confidence_gate(fields, _schema())
    assert len(items) == 1
    assert "根拠 span なし" in items[0].reason


def test_always_review_field() -> None:
    fields = [ExtractedField(name="note", confidence=0.99, grounding_score=1.0)]
    items = confidence_gate(fields, _schema(), always_review_fields={"note"})
    assert len(items) == 1
    assert "always_review" in items[0].reason


def test_all_auto_when_above_threshold() -> None:
    fields = [
        ExtractedField(name="total_amount", confidence=0.95, grounding_score=1.0),
        ExtractedField(name="note", confidence=0.90, grounding_score=1.0),
    ]
    assert confidence_gate(fields, _schema()) == []
