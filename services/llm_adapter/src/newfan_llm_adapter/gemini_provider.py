# mypy: ignore-errors
"""Gemini プロバイダ（google-genai SDK）。LLMProvider Protocol 実装（プロバイダ切替, §2.1）。

runtime extra（google-genai）が必要。API キーは GEMINI_API_KEY / GOOGLE_API_KEY。
既定モデルは gemini-2.5-flash。JSON 契約はプロンプト側で担保（kie/correct が```で括られた
出力も許容）。
"""

from __future__ import annotations

import os
from typing import Any, Optional

from newfan_llm_adapter.provider import LLMResponse

DEFAULT_MODEL = "gemini-2.5-flash"


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
            config={"system_instruction": system, "max_output_tokens": max_tokens},
        )
        text = resp.text or ""
        um = getattr(resp, "usage_metadata", None)
        return LLMResponse(
            text=text,
            input_tokens=getattr(um, "prompt_token_count", 0) or 0,
            output_tokens=getattr(um, "candidates_token_count", 0) or 0,
            model=self._model,
        )
