"""memory_lookup/learn ノードの実体化と、deterministic_normalize のテナントルール適用。"""

from newfan_memory import (
    HashingEmbedder,
    InMemoryMemoryRepository,
    MemoryService,
    RuleStatus,
    RuleType,
    TenantRule,
)
from newfan_schemas import ExtractedField

from newfan_orchestrator import memory_nodes, nodes


def _service() -> MemoryService:
    return MemoryService(HashingEmbedder(), InMemoryMemoryRepository())


def _add_active_rule(service: MemoryService) -> None:
    service._repo.add_rule(  # type: ignore[attr-defined]
        TenantRule(
            id="rul_1",
            tenant_id="ten_1",
            doc_type="invoice",
            field_name="total_amount",
            rule_type=RuleType.REGEX_REPLACE,
            rule_json={"pattern": "O", "replacement": "0"},
            status=RuleStatus.ACTIVE,
        )
    )


def test_memory_lookup_node_returns_active_rules() -> None:
    service = _service()
    _add_active_rule(service)
    node = memory_nodes.make_memory_lookup(service)
    out = node({"tenant_id": "ten_1", "schema": {"doc_type": "invoice"}, "layout_markdown": "請求書"})
    assert out["active_rules"]
    assert out["active_rules"][0]["rule_type"] == "regex_replace"


def test_learn_node_registers_corrections() -> None:
    service = _service()
    node = memory_nodes.make_learn(service)
    state = {
        "tenant_id": "ten_1",
        "document_id": "doc_1",
        "run_id": "run_1",
        "schema": {"doc_type": "invoice"},
        "human_feedback": {
            "corrections": [
                {"field_name": "total_amount", "original_value": "12O", "corrected_value": "120"}
            ]
        },
    }
    node(state)
    # 登録され、検索で引ける
    hits = service.search(
        tenant_id="ten_1",
        doc_type="invoice",
        supplier="",
        field_name="total_amount",
        value_raw="12O",
        context="",
    )
    assert hits and hits[0]["to"] == "120"


def test_deterministic_normalize_applies_tenant_rule() -> None:
    # 文字列フィールドに vocab_map ルールを適用（固有名詞の写像）。
    # 組込み正規化（NFKC/trim）→ テナントルール の順（§4.3）。
    field = ExtractedField(name="issuer_name", value_raw="カ)サンプル")
    schema = {"doc_type": "invoice", "fields": [{"name": "issuer_name", "type": "string"}]}
    active_rules = [
        {
            "id": "rul_1",
            "tenant_id": "ten_1",
            "doc_type": "invoice",
            "field_name": "issuer_name",
            "rule_type": "vocab_map",
            "rule_json": {"from": "カ)サンプル", "to": "株式会社サンプル"},
            "status": "active",
        }
    ]
    out = nodes.deterministic_normalize(
        {"schema": schema, "fields": [field], "active_rules": active_rules}
    )
    assert out["fields"][0].value_normalized == "株式会社サンプル"


def test_deterministic_normalize_skips_invalid_rule() -> None:
    field = ExtractedField(name="total_amount", value_raw="128000")
    schema = {"doc_type": "invoice", "fields": [{"name": "total_amount", "type": "money_jpy"}]}
    bad_rule = [{"not": "a valid rule"}]
    out = nodes.deterministic_normalize(
        {"schema": schema, "fields": [field], "active_rules": bad_rule}
    )
    assert out["fields"][0].value_normalized == "128000"  # 壊れず通過
