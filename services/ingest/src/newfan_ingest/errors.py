from __future__ import annotations


class IngestError(Exception):
    """取込エラー。code は §6.5 のエラーコード体系（E1001/E1002/E1003）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
