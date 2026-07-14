"""confidence ゲート判定（詳細設計 §2.5 / §4.3 confidence_gate）。

閾値表とテナントの always_review 設定から review_items を生成する純ロジック。
"""

from __future__ import annotations

from dataclasses import dataclass

from newfan_schemas import ExtractedField, FieldSchema, ReviewItem


@dataclass(frozen=True)
class Thresholds:
    """§2.5 主要設定パラメータ（初期値・仮置き）。テナント設定で上書き可。"""

    critical: float = 0.90
    standard: float = 0.80
    low_impact: float = 0.60


def threshold_for(field_critical: bool, thresholds: Thresholds) -> float:
    return thresholds.critical if field_critical else thresholds.standard


def confidence_gate(
    fields: list[ExtractedField],
    schema: FieldSchema,
    *,
    thresholds: Thresholds | None = None,
    always_review_fields: set[str] | None = None,
) -> list[ReviewItem]:
    """レビュー要フィールドを ReviewItem 列として返す。

    レビュー条件:
    - always_review_fields に含まれる
    - grounding_score == 0（根拠 span なし → 強制レビュー, §5.7.2）
    - confidence < 閾値（critical/standard）
    """
    thresholds = thresholds or Thresholds()
    always = always_review_fields or set()
    critical_names = schema.critical_field_names()

    items: list[ReviewItem] = []
    for field in fields:
        is_critical = field.name in critical_names
        threshold = threshold_for(is_critical, thresholds)

        reason: str | None = None
        if field.name in always:
            reason = "always_review 指定"
        elif field.grounding_score <= 0.0:
            reason = "根拠 span なし（強制レビュー）"
        elif field.confidence < threshold:
            reason = f"confidence {field.confidence:.2f} < 閾値 {threshold:.2f}"

        if reason is not None:
            items.append(
                ReviewItem(
                    field_name=field.name,
                    reason=reason,
                    confidence=field.confidence,
                    critical=is_critical,
                    page=field.page,
                    bbox=field.bbox,
                )
            )
    return items
