from __future__ import annotations

from typing import Optional


class LLMError(Exception):
    """LLM 呼出しの失敗。code は §6.5（E3001 タイムアウト/レート、E3002 JSON契約違反）。"""

    def __init__(self, code: str, message: str, *, detail: Optional[str] = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.detail = detail
