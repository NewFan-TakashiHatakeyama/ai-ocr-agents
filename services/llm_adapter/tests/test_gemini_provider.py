"""GeminiProvider の単体テスト（google-genai client は Fake で注入）。"""

from __future__ import annotations

from types import SimpleNamespace

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


def test_gemini_handles_missing_usage() -> None:
    client = SimpleNamespace(
        models=SimpleNamespace(
            generate_content=lambda *, model, contents, config: SimpleNamespace(text="hi", usage_metadata=None)
        )
    )
    r = GeminiProvider(client=client).complete(system="s", user="u", max_tokens=10)
    assert r.text == "hi" and r.input_tokens == 0 and r.output_tokens == 0
