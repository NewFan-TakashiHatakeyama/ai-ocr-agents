"""ファイル検証（§5.1 受理形式・制限、§11 マジックバイト検証）。

純ロジック（外部依存なし）。E1001/E1002/E1003 を送出する。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from newfan_ingest.errors import IngestError

Kind = Literal["pdf", "png", "jpeg", "tiff", "office"]

# マジックバイト → 種別（§11 拡張子照合と併用）
_MAGIC: list[tuple[bytes, Kind]] = [
    (b"%PDF-", "pdf"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"II*\x00", "tiff"),
    (b"MM\x00*", "tiff"),
    (b"PK\x03\x04", "office"),  # DOCX/XLSX/PPTX（ZIP コンテナ）
]

_OFFICE_EXTS = {".docx", ".xlsx", ".pptx"}
_EXT_TO_KIND: dict[str, Kind] = {
    ".pdf": "pdf",
    ".png": "png",
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
    ".tif": "tiff",
    ".tiff": "tiff",
    ".docx": "office",
    ".xlsx": "office",
    ".pptx": "office",
}


@dataclass(frozen=True)
class IngestLimits:
    max_bytes: int = 50 * 1024 * 1024  # 50MB/ファイル
    max_pages: int = 300  # 300ページ/ドキュメント
    max_side_px: int = 4000  # 長辺上限（超過は縮小）


def detect_kind(content: bytes, filename: str) -> Optional[Kind]:
    """マジックバイトで種別を判定。ZIP は拡張子で Office を確定する。"""
    kind: Optional[Kind] = None
    for magic, k in _MAGIC:
        if content.startswith(magic):
            kind = k
            break
    if kind is None:
        return None
    ext = _ext(filename)
    if kind == "office":
        # ZIP コンテナは Office 拡張子のときのみ Office と認める
        return "office" if ext in _OFFICE_EXTS else None
    return kind


def _ext(filename: str) -> str:
    dot = filename.rfind(".")
    return filename[dot:].lower() if dot != -1 else ""


def validate_upload(content: bytes, filename: str, limits: IngestLimits | None = None) -> Kind:
    """検証して種別を返す。不合格は IngestError（E1001/E1002）。

    Office は後段で LibreOffice により PDF 化する（変換失敗は E1003, service 側）。
    """
    limits = limits or IngestLimits()

    if len(content) == 0:
        raise IngestError("E1001", "空ファイルです")
    if len(content) > limits.max_bytes:
        raise IngestError("E1002", f"サイズ上限 {limits.max_bytes} バイトを超えています")

    kind = detect_kind(content, filename)
    if kind is None:
        raise IngestError("E1001", "非対応または不整合なファイル形式です（マジックバイト/拡張子）")

    # 拡張子とマジックの整合（ポリグロット対策の一次防御, §11）
    ext = _ext(filename)
    expected = _EXT_TO_KIND.get(ext)
    if expected is not None and expected != kind:
        raise IngestError("E1001", f"拡張子({ext})と実体({kind})が一致しません")

    return kind
