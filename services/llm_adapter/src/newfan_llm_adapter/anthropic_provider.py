# mypy: ignore-errors
"""AnthropicProvider（公式 anthropic SDK）。

optional-dependency（anthropic extra）。既定モデルは claude-opus-4-8。KIE/補正は構造化
抽出タスクのため thinking は既定オフ（低レイテンシ・低コスト）。JSON 契約はプロンプトで担保し、
adapter 側で 1 回リペアリトライする（§10 / E3002）。認証は環境の ANTHROPIC_API_KEY / プロファイル。
"""

from __future__ import annotations

from typing import Optional

from newfan_llm_adapter.provider import LLMResponse

DEFAULT_MODEL = "claude-opus-4-8"


class AnthropicProvider:
    def __init__(self, *, model: str = DEFAULT_MODEL, client: Optional[object] = None) -> None:
        self._model = model
        if client is not None:
            self._client = client
        else:
            import anthropic

            self._client = anthropic.Anthropic()

    def complete(self, *, system: str, user: str, max_tokens: int) -> LLMResponse:
        msg = self._client.messages.create(
            model=self._model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
        usage = getattr(msg, "usage", None)
        return LLMResponse(
            text=text,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            model=getattr(msg, "model", self._model),
        )
