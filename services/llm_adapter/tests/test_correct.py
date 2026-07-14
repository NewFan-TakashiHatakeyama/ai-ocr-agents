import json

from newfan_llm_adapter import FakeProvider, LLMAdapter, PromptBundle, llm_correct


def _correct_response(**kw: object) -> str:
    base = {
        "corrected": "128000",
        "changed": True,
        "needs_review": False,
        "used_pairs": [],
        "memory_refs": [],
        "rationale": "",
        "confidence": 0.9,
    }
    base.update(kw)
    return json.dumps(base, ensure_ascii=False)


def _correct(bundle: PromptBundle, resp: str):
    adapter = LLMAdapter(FakeProvider([resp]))
    return llm_correct(
        adapter,
        bundle,
        field_name="total_amount",
        field_type="money_jpy",
        fmt="digits",
        value_raw="128,OOO",
        char_confs=[0.9, 0.9, 0.5],
        context="御請求金額 128,OOO",
    )


def test_correction_applied_when_pairs_in_confusion_table(bundle: PromptBundle) -> None:
    # O↔0 は混同文字表にある → 自動適用可
    result = _correct(bundle, _correct_response(used_pairs=[["O", "0"]]))
    assert result.applied is True
    assert result.needs_review is False
    assert result.corrected == "128000"


def test_correction_blocked_when_pair_not_allowed(bundle: PromptBundle) -> None:
    # 1↔9 は混同文字表にない → DD-10 違反 → 自動適用せず needs_review
    result = _correct(bundle, _correct_response(used_pairs=[["1", "9"]]))
    assert result.applied is False
    assert result.needs_review is True


def test_correction_allowed_via_memory_ref(bundle: PromptBundle) -> None:
    # 混同表外でもテナント修正メモリ一致（memory_refs あり）なら許可
    result = _correct(
        bundle, _correct_response(used_pairs=[["A", "B"]], memory_refs=["cor_1"])
    )
    assert result.applied is True


def test_correction_not_applied_when_unchanged(bundle: PromptBundle) -> None:
    result = _correct(bundle, _correct_response(changed=False, used_pairs=[["O", "0"]]))
    assert result.applied is False


def test_correction_blocked_when_no_evidence(bundle: PromptBundle) -> None:
    # changed=True だが used_pairs も memory_refs も無い → 自動適用不可
    result = _correct(bundle, _correct_response(used_pairs=[], memory_refs=[]))
    assert result.applied is False
    assert result.needs_review is True
