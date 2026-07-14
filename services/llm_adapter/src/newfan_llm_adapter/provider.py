"""LLM プロバイダ抽象（§2.1 llm-adapter）。

設計の「プロバイダ切替」を Protocol で表現する。既定の本番実装は AnthropicProvider
（公式 anthropic SDK, claude-opus-4-8）。テストは FakeProvider。
"""

from __future__ import annotations

from typing import Callable, Optional, Protocol, Union

from pydantic import BaseModel


class LLMResponse(BaseModel):
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""


class LLMProvider(Protocol):
    def complete(self, *, system: str, user: str, max_tokens: int) -> LLMResponse:
        """system/user から 1 応答を返す。JSON 契約はプロンプト側で担保する。"""
        ...


FakeHandler = Callable[[str, str], Union[str, LLMResponse]]


class FakeProvider:
    """テスト用。固定応答列 or ハンドラ関数を受ける。"""

    def __init__(
        self,
        responses: Optional[list[Union[str, LLMResponse]]] = None,
        *,
        handler: Optional[FakeHandler] = None,
        model: str = "fake-model",
    ) -> None:
        self._responses = list(responses or [])
        self._handler = handler
        self._model = model
        self.calls: list[tuple[str, str]] = []

    def complete(self, *, system: str, user: str, max_tokens: int) -> LLMResponse:
        self.calls.append((system, user))
        if self._handler is not None:
            out = self._handler(system, user)
        elif self._responses:
            out = self._responses.pop(0)
        else:
            raise AssertionError("FakeProvider の応答が尽きました")
        if isinstance(out, LLMResponse):
            return out
        return LLMResponse(text=out, model=self._model)
