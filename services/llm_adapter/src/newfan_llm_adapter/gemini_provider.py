# mypy: ignore-errors
"""Gemini プロバイダ（google-genai SDK）。LLMProvider Protocol 実装（プロバイダ切替, §2.1）。

runtime extra（google-genai）が必要。API キーは GEMINI_API_KEY / GOOGLE_API_KEY。
既定モデルは gemini-2.5-flash。JSON 契約はプロンプト側で担保（kie/correct が```で括られた
出力も許容）。KIE/補正は構造化抽出タスクのため thinking は既定オフ（AnthropicProvider と同方針）。
"""

from __future__ import annotations

import os
from typing import Any, Optional

from newfan_llm_adapter.errors import LLMError
from newfan_llm_adapter.provider import LLMResponse

DEFAULT_MODEL = "gemini-2.5-flash"


def _raise_if_truncated(resp: Any) -> None:
    """max_output_tokens 到達で打ち切られた応答を明示的に失敗させる。

    黙って返すと途中で切れた JSON が adapter で「E3002 JSON 契約違反」として報告され、
    原因（予算不足）が分からなくなる（実際に診断を誤らせた）。
    """
    candidates = getattr(resp, "candidates", None) or []
    if not candidates:
        return
    finish = getattr(candidates[0], "finish_reason", None)
    if finish is not None and getattr(finish, "name", str(finish)) == "MAX_TOKENS":
        um = getattr(resp, "usage_metadata", None)
        raise LLMError(
            "E3002",
            "Gemini 応答が max_output_tokens で打ち切られました（予算不足）",
            detail=(
                f"candidates_token_count={getattr(um, 'candidates_token_count', None)} "
                f"thoughts_token_count={getattr(um, 'thoughts_token_count', None)}"
            ),
        )


class GeminiProvider:
    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        client: Optional[Any] = None,
        api_key: Optional[str] = None,
    ) -> None:
        self._model = model
        if client is not None:
            self._client = client
        else:
            from google import genai  # 遅延 import（runtime extra）

            key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
            self._client = genai.Client(api_key=key)

    def complete(self, *, system: str, user: str, max_tokens: int) -> LLMResponse:
        resp = self._client.models.generate_content(
            model=self._model,
            contents=user,
            config={
                "system_instruction": system,
                "max_output_tokens": max_tokens,
                # Gemini 2.5 は thinking が既定 ON で、thinking tokens が max_output_tokens を
                # 消費する。実測（sample2.png / 8192 予算）では thoughts=7860 を使い切り本文が
                # 327 トークンで打ち切られ、途中で切れた JSON が E3002 として報告された。
                # thinking を切ると本文に全予算が回る。Anthropic 側と同じく KIE は thinking オフ。
                "thinking_config": {"thinking_budget": 0},
            },
        )
        _raise_if_truncated(resp)
        text = resp.text or ""
        um = getattr(resp, "usage_metadata", None)
        return LLMResponse(
            text=text,
            input_tokens=getattr(um, "prompt_token_count", 0) or 0,
            output_tokens=getattr(um, "candidates_token_count", 0) or 0,
            model=self._model,
        )
