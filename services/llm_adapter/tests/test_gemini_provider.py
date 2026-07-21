"""GeminiProvider の単体テスト（google-genai client は Fake で注入）。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from newfan_llm_adapter.errors import LLMError
from newfan_llm_adapter.gemini_provider import GeminiProvider
from newfan_llm_adapter.provider import LLMResponse


def _fake_client(text: str, pin: int = 5, pout: int = 2) -> SimpleNamespace:
    captured: dict = {}

    def generate_content(*, model, contents, config):
        captured["model"] = model
        captured["contents"] = contents
        captured["config"] = config
        return SimpleNamespace(
            text=text,
            usage_metadata=SimpleNamespace(prompt_token_count=pin, candidates_token_count=pout),
        )

    client = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    client._captured = captured  # type: ignore[attr-defined]
    return client


def test_gemini_complete_maps_to_llmresponse() -> None:
    client = _fake_client('{"fields": []}')
    p = GeminiProvider(model="gemini-2.5-flash", client=client)
    r = p.complete(system="抽出してください", user="spans...", max_tokens=1024)

    assert isinstance(r, LLMResponse)
    assert r.text == '{"fields": []}'
    assert r.input_tokens == 5 and r.output_tokens == 2
    assert r.model == "gemini-2.5-flash"
    # system は system_instruction に、user は contents に渡る
    cap = client._captured
    assert cap["config"]["system_instruction"] == "抽出してください"
    assert cap["contents"] == "spans..." and cap["config"]["max_output_tokens"] == 1024


def test_gemini_disables_thinking() -> None:
    """Gemini 2.5 の thinking は max_output_tokens を食う（実測: 8192 中 7860）。

    KIE は構造化抽出なので thinking はオフ（Anthropic 側と同方針）。ON のままだと本文が
    数百トークンで打ち切られ、壊れた JSON が E3002 になる。
    """
    client = _fake_client('{"fields": []}')
    GeminiProvider(client=client).complete(system="s", user="u", max_tokens=8192)
    assert client._captured["config"]["thinking_config"] == {"thinking_budget": 0}


def test_gemini_raises_on_max_tokens_truncation() -> None:
    """打ち切りは「JSON 契約違反」ではなく予算不足として報告する。"""
    resp = SimpleNamespace(
        text='{"fields": [',
        candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name="MAX_TOKENS"))],
        usage_metadata=SimpleNamespace(
            prompt_token_count=4642, candidates_token_count=327, thoughts_token_count=7860
        ),
    )
    client = SimpleNamespace(
        models=SimpleNamespace(generate_content=lambda *, model, contents, config: resp)
    )
    with pytest.raises(LLMError) as ei:
        GeminiProvider(client=client).complete(system="s", user="u", max_tokens=8192)
    assert ei.value.code == "E3002"
    assert "打ち切られました" in ei.value.message
    assert "thoughts_token_count=7860" in (ei.value.detail or "")


def test_gemini_accepts_normal_finish_reason() -> None:
    resp = SimpleNamespace(
        text="ok",
        candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name="STOP"))],
        usage_metadata=None,
    )
    client = SimpleNamespace(
        models=SimpleNamespace(generate_content=lambda *, model, contents, config: resp)
    )
    assert GeminiProvider(client=client).complete(system="s", user="u", max_tokens=10).text == "ok"


def test_gemini_handles_missing_usage() -> None:
    client = SimpleNamespace(
        models=SimpleNamespace(
            generate_content=lambda *, model, contents, config: SimpleNamespace(text="hi", usage_metadata=None)
        )
    )
    r = GeminiProvider(client=client).complete(system="s", user="u", max_tokens=10)
    assert r.text == "hi" and r.input_tokens == 0 and r.output_tokens == 0
