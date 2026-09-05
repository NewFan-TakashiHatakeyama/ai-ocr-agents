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


# --- confidence_gate_node: 所見が人に届くこと（Phase 0 の既存バグ修正） ---
#
# confidence_gate 自体は純ロジックで正しく所見を返していたが、ノード側で
# 2 つ落ちていた: (1) 上流の ReviewItem を置換で捨てる (2) 所見の付いた
# field を pending にしないため検証画面の「要確認」に出ない。


def test_review_items_mark_fields_pending() -> None:
    """gate の所見が付いた field は pending になる（要確認に出る）。

    検証画面は extraction_fields.review_status しか見ないため、ここで
    PENDING を立てないと「run は needs_review なのに画面は全部確定済み」に
    見える（確信度 0.00 の項目が確定済みとして並ぶ実症状）。
    """
    from newfan_schemas import ReviewStatus

    from newfan_orchestrator import nodes

    low = ExtractedField(name="total_amount", value_raw="1", span_ids=[1],
                         confidence=0.10, grounding_score=1.0)
    ok = ExtractedField(name="issuer_name", value_raw="x", span_ids=[2],
                        confidence=0.99, grounding_score=1.0)
    out = nodes.confidence_gate_node(
        {"schema": {"doc_type": "invoice", "fields": []}, "fields": [low, ok]}
    )
    by = {f.name: f for f in out["fields"]}
    assert by["total_amount"].review_status is ReviewStatus.PENDING
    assert by["issuer_name"].review_status is ReviewStatus.AUTO  # 所見なしは触らない
    assert [i.field_name for i in out["review_items"]] == ["total_amount"]


def test_gate_does_not_downgrade_human_confirmed_fields() -> None:
    """人手確定（corrected/approved）は pending に差し戻さない。

    resume 後の apply_feedback で確定した値を、再走時の gate が
    「まだ確信度が低い」という理由で未確認へ戻すと人の作業が消える。
    """
    from newfan_schemas import ReviewStatus

    from newfan_orchestrator import nodes

    fixed = ExtractedField(name="total_amount", value_raw="1", span_ids=[1],
                           confidence=0.10, grounding_score=1.0,
                           review_status=ReviewStatus.CORRECTED)
    out = nodes.confidence_gate_node(
        {"schema": {"doc_type": "invoice", "fields": []}, "fields": [fixed]}
    )
    assert out["fields"][0].review_status is ReviewStatus.CORRECTED


def test_gate_carries_forward_vl_fallback_review_items() -> None:
    """上流（vl_fallback）の ReviewItem を捨てない。

    review_items は reducer 無しの LastValue チャネルなので、gate が置換
    return すると「VL 失敗で未抽出のページ」の警告がグラフ通過時に消える。
    ノード単体テストは戻り値しか見ないため長く検出できていなかった。
    """
    from newfan_schemas import ReviewItem

    from newfan_orchestrator import nodes

    upstream = ReviewItem(field_name="__page_2", reason="VL結果なし（未抽出ページ）")
    low = ExtractedField(name="total_amount", value_raw="1", span_ids=[1],
                         confidence=0.10, grounding_score=1.0)
    out = nodes.confidence_gate_node(
        {
            "schema": {"doc_type": "invoice", "fields": []},
            "fields": [low],
            "review_items": [upstream],
        }
    )
    names = [i.field_name for i in out["review_items"]]
    assert names == ["__page_2", "total_amount"]  # 上流が先、gate 所見が後
    assert nodes.route_confidence_gate(out) == "hitl_review"


def test_gate_dedups_identical_review_items() -> None:
    """同一 (field_name, reason) は重複させない（再走で二重に見せない）。"""
    from newfan_orchestrator import nodes

    low = ExtractedField(name="total_amount", value_raw="1", span_ids=[1],
                         confidence=0.10, grounding_score=1.0)
    once = nodes.confidence_gate_node(
        {"schema": {"doc_type": "invoice", "fields": []}, "fields": [low]}
    )
    twice = nodes.confidence_gate_node(
        {
            "schema": {"doc_type": "invoice", "fields": []},
            "fields": [low],
            "review_items": once["review_items"],
        }
    )
    assert len(twice["review_items"]) == len(once["review_items"]) == 1
