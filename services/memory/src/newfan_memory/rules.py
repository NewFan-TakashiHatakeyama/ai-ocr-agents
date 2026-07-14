"""ルール適用エンジンと自動検証・ライフサイクル（§5.8.4）。

適用対象の変換は regex_replace / vocab_map のみ。format / checksum / llm_hint は検証・ヒント用で
値を変換しない（no-op）。自動検証は「過去修正の90%以上を再現」かつ「確定済み正解値への誤適用0件」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from newfan_memory.records import RuleStatus, RuleType, TenantRule

MIN_REPRODUCTION = 0.90  # §2.5 rules.validation_pass


def apply_rule(rule: TenantRule, value: str) -> str:
    """ルールを値に適用して変換後の値を返す（変換しないルール型は value をそのまま返す）。"""
    j = rule.rule_json
    if rule.rule_type is RuleType.REGEX_REPLACE:
        pattern = j.get("pattern")
        replacement = j.get("replacement", "")
        if not pattern:
            return value
        try:
            return re.sub(pattern, replacement, value)
        except re.error:
            return value
    if rule.rule_type is RuleType.VOCAB_MAP:
        # 固有名詞・勘定科目の写像（完全一致）
        return j.get("to", value) if value == j.get("from") else value
    # format / checksum / llm_hint は変換しない
    return value


@dataclass
class ValidationReport:
    reproduction_rate: float
    false_applications: int
    sample_size: int
    passed: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "reproduction_rate": self.reproduction_rate,
            "false_applications": self.false_applications,
            "sample_size": self.sample_size,
            "passed": self.passed,
        }


def validate_rule(
    rule: TenantRule,
    corrections: list[tuple[str, str]],
    confirmed_values: list[str],
    *,
    min_reproduction: float = MIN_REPRODUCTION,
) -> ValidationReport:
    """§5.8.4 の自動検証。

    - corrections: (original, corrected) のホールドアウト。ルール適用で corrected を再現できた割合。
    - confirmed_values: 確定済み正解値。ルールが 1 件でも変えたら誤適用（false application）。
    """
    reproduced = sum(1 for frm, to in corrections if apply_rule(rule, frm) == to)
    rate = reproduced / len(corrections) if corrections else 0.0
    false_apps = sum(1 for v in confirmed_values if apply_rule(rule, v) != v)
    passed = rate >= min_reproduction and false_apps == 0
    return ValidationReport(
        reproduction_rate=rate,
        false_applications=false_apps,
        sample_size=len(corrections),
        passed=passed,
    )


def finalize_status(rule: TenantRule, report: ValidationReport) -> RuleStatus:
    """検証結果からルールの次状態を決める。合格→active、不合格→draft のまま。"""
    return RuleStatus.ACTIVE if report.passed else RuleStatus.DRAFT
