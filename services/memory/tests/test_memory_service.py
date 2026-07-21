import json

from newfan_llm_adapter import FakeProvider, LLMAdapter, PromptBundle, default_bundle_dir

from newfan_memory import (
    CorrectionLog,
    HashingEmbedder,
    InMemoryMemoryRepository,
    MemoryService,
    RuleStatus,
)

_BUNDLE = PromptBundle.load(default_bundle_dir())


def _service(adapter: LLMAdapter | None = None, *, min_evidence: int = 5) -> MemoryService:
    return MemoryService(
        HashingEmbedder(),
        InMemoryMemoryRepository(),
        adapter=adapter,
        bundle=_BUNDLE if adapter else None,
        min_evidence=min_evidence,
    )


def _correction(i: int, *, frm: str, to: str) -> CorrectionLog:
    return CorrectionLog(
        id=f"cor_{i}",
        tenant_id="ten_1",
        document_id="doc_1",
        run_id="run_1",
        field_name="total_amount",
        original_value=frm,
        corrected_value=to,
        doc_type="invoice",
        supplier_key="A社",
        context="御請求金額",
    )


def test_add_and_search_retrieves_similar() -> None:
    svc = _service()
    svc.add(_correction(1, frm="128,OOO", to="128,000"))
    results = svc.search(
        tenant_id="ten_1",
        doc_type="invoice",
        supplier="A社",
        field_name="total_amount",
        value_raw="256,OOO",
        context="御請求金額",
    )
    assert results  # sim>=0.75 の類似例が返る
    assert results[0]["to"] == "128,000"


def test_index_rehydrated_from_shared_repo() -> None:
    """別プロセス相当（新 index）でも正本(repo)から index を再構築し検索できる（§5.8.3）。"""
    repo = InMemoryMemoryRepository()
    MemoryService(HashingEmbedder(), repo).add(_correction(1, frm="128,OOO", to="128,000"))

    # 新しい MemoryService（index は空）。search 時に _rehydrate で repo から復元される。
    fresh = MemoryService(HashingEmbedder(), repo)
    results = fresh.search(
        tenant_id="ten_1",
        doc_type="invoice",
        supplier="A社",
        field_name="total_amount",
        value_raw="256,OOO",
        context="御請求金額",
    )
    assert results and results[0]["to"] == "128,000"


def test_search_filters_below_threshold() -> None:
    svc = _service()
    svc.add(_correction(1, frm="128,OOO", to="128,000"))
    # 全く異なる文脈 → 閾値未満で 0 件
    results = svc.search(
        tenant_id="ten_1",
        doc_type="order",
        supplier="Z社",
        field_name="qty",
        value_raw="3",
        context="数量",
    )
    assert results == []


def test_tenant_isolation_in_search() -> None:
    svc = _service()
    svc.add(_correction(1, frm="128,OOO", to="128,000"))
    other = svc.search(
        tenant_id="ten_2",
        doc_type="invoice",
        supplier="A社",
        field_name="total_amount",
        value_raw="128,OOO",
        context="御請求金額",
    )
    assert other == []


def test_learn_triggers_rule_extraction_and_activates() -> None:
    # LLM が O→0 の regex_replace を返す → 検証合格で active
    rule_json = [
        {
            "rule_type": "regex_replace",
            "pattern": "O",
            "replacement": "0",
            "evidence_ids": ["cor_1"],
            "doc_type": "invoice",
            "field": "total_amount",
        }
    ]
    payload = json.dumps(rule_json)
    adapter = LLMAdapter(FakeProvider(handler=lambda s, u: payload))
    svc = _service(adapter, min_evidence=3)

    # 4件の同種修正（O→0 パターン）
    result = None
    for i, (frm, to) in enumerate(
        [("12O", "120"), ("O5O", "050"), ("1OO", "100"), ("9O", "90")], start=1
    ):
        result = svc.learn(
            tenant_id="ten_1",
            document_id="doc_1",
            run_id="run_1",
            field_name="total_amount",
            original_value=frm,
            corrected_value=to,
            doc_type="invoice",
            supplier_key="A社",
            context="御請求金額",
        )
    assert result is not None
    assert result.rule_extraction_triggered is True
    assert any(r.status is RuleStatus.ACTIVE for r in result.new_rules)
    # active_rules で取得できる
    active = svc.active_rules("ten_1", "invoice")
    assert active and active[0].rule_type.value == "regex_replace"


def test_learn_below_evidence_does_not_extract() -> None:
    adapter = LLMAdapter(FakeProvider([]))
    svc = _service(adapter, min_evidence=5)
    result = svc.learn(
        tenant_id="ten_1",
        document_id="doc_1",
        run_id="run_1",
        field_name="total_amount",
        original_value="12O",
        corrected_value="120",
        doc_type="invoice",
    )
    assert result.rule_extraction_triggered is False


def test_learn_rule_that_breaks_confirmed_stays_draft() -> None:
    # LLM が過剰一般化ルール（数字を全部消す）を返す → 確定値を壊す → draft 据え置き
    bad_rule = [
        {"rule_type": "regex_replace", "pattern": r"\d", "replacement": "", "evidence_ids": []}
    ]
    adapter = LLMAdapter(FakeProvider([json.dumps(bad_rule)]))
    svc = _service(adapter, min_evidence=2)
    result = None
    for frm, to in [("12O", "120"), ("O5O", "050")]:
        result = svc.learn(
            tenant_id="ten_1",
            document_id="d",
            run_id="r",
            field_name="total_amount",
            original_value=frm,
            corrected_value=to,
            doc_type="invoice",
        )
    assert result is not None
    assert all(r.status is RuleStatus.DRAFT for r in result.new_rules)
    assert svc.active_rules("ten_1", "invoice") == []
