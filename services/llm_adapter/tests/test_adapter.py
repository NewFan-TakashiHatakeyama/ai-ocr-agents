import pytest

from newfan_llm_adapter import FakeProvider, LLMAdapter, LLMError, estimate_cost_usd
from newfan_llm_adapter.provider import LLMResponse


def test_complete_json_parses() -> None:
    provider = FakeProvider(['{"a": 1}'])
    adapter = LLMAdapter(provider)
    data, resp = adapter.complete_json(system="s", user="u")
    assert data == {"a": 1}


def test_complete_json_extracts_from_prose() -> None:
    provider = FakeProvider(['ここに結果です:\n{"a": 2}\n以上'])
    adapter = LLMAdapter(provider)
    data, _ = adapter.complete_json(system="s", user="u")
    assert data == {"a": 2}


def test_complete_json_repairs_once() -> None:
    provider = FakeProvider(["not json at all", '{"ok": true}'])
    adapter = LLMAdapter(provider)
    data, _ = adapter.complete_json(system="s", user="u")
    assert data == {"ok": True}
    assert len(provider.calls) == 2  # 1 回リペアした


def test_complete_json_e3002_after_retry() -> None:
    provider = FakeProvider(["nope", "still nope"])
    adapter = LLMAdapter(provider)
    with pytest.raises(LLMError) as exc:
        adapter.complete_json(system="s", user="u")
    assert exc.value.code == "E3002"


def test_usage_and_cost_accounted() -> None:
    provider = FakeProvider(
        [LLMResponse(text='{"a":1}', input_tokens=1000, output_tokens=200, model="claude-opus-4-8")]
    )
    adapter = LLMAdapter(provider, model="claude-opus-4-8")
    adapter.complete_json(system="s", user="u")
    assert adapter.total_input_tokens == 1000
    assert adapter.total_output_tokens == 200
    assert adapter.total_cost_usd == pytest.approx(estimate_cost_usd("claude-opus-4-8", 1000, 200))


def test_zdr_guard_blocks_fable() -> None:
    # ZDR 必須テナントで fable-5（ZDR非対応）は拒否
    with pytest.raises(LLMError):
        LLMAdapter(FakeProvider([]), model="claude-fable-5", zdr_required=True)


def test_zdr_guard_allows_opus() -> None:
    LLMAdapter(FakeProvider([]), model="claude-opus-4-8", zdr_required=True)
