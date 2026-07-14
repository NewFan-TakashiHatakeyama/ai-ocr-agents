from newfan_memory import RuleStatus, RuleType, TenantRule, apply_rule, finalize_status, validate_rule


def _regex_rule(pattern: str, replacement: str) -> TenantRule:
    return TenantRule(
        id="rul_1",
        tenant_id="ten_1",
        rule_type=RuleType.REGEX_REPLACE,
        rule_json={"pattern": pattern, "replacement": replacement},
    )


def test_apply_regex_replace() -> None:
    rule = _regex_rule("O", "0")
    assert apply_rule(rule, "128,OOO") == "128,000"


def test_apply_vocab_map_exact() -> None:
    rule = TenantRule(
        id="r", tenant_id="t", rule_type=RuleType.VOCAB_MAP, rule_json={"from": "㈱A", "to": "株式会社A"}
    )
    assert apply_rule(rule, "㈱A") == "株式会社A"
    assert apply_rule(rule, "別") == "別"  # 完全一致のみ


def test_apply_noop_types() -> None:
    for rt in (RuleType.FORMAT, RuleType.CHECKSUM, RuleType.LLM_HINT):
        rule = TenantRule(id="r", tenant_id="t", rule_type=rt, rule_json={"x": 1})
        assert apply_rule(rule, "unchanged") == "unchanged"


def test_validate_pass_activates() -> None:
    rule = _regex_rule("O", "0")
    corrections = [("12O", "120"), ("O5O", "050"), ("1OO", "100")]
    confirmed = ["999", "128000"]  # O を含まない → 誤適用0
    report = validate_rule(rule, corrections, confirmed)
    assert report.reproduction_rate == 1.0
    assert report.false_applications == 0
    assert report.passed is True
    assert finalize_status(rule, report) is RuleStatus.ACTIVE


def test_validate_false_application_blocks() -> None:
    rule = _regex_rule("O", "0")
    corrections = [("12O", "120")]
    confirmed = ["CODE-O7"]  # 正解値に O が含まれる → ルールが壊す → 誤適用
    report = validate_rule(rule, corrections, confirmed)
    assert report.false_applications == 1
    assert report.passed is False
    assert finalize_status(rule, report) is RuleStatus.DRAFT


def test_validate_low_reproduction_blocks() -> None:
    rule = _regex_rule("O", "0")
    # 大半が再現できない
    corrections = [("12O", "120"), ("abc", "xyz"), ("def", "ghi"), ("jkl", "mno")]
    report = validate_rule(rule, corrections, [])
    assert report.reproduction_rate < 0.90
    assert report.passed is False
