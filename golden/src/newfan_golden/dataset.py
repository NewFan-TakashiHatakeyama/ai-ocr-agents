"""ゴールデンデータセットの読み書き（§14.2）。

形式は JSONL（1 行 = 1 ドキュメント）。fields の正解値は**正規化後**の表現で保持する。
実データは S3 の専用バケットでバージョン管理する（本リポジトリには置かない。
帳票は顧客データであり、git に入れると消せなくなるため）。

1 行の形:
  {
    "document_id": "gold_0001",
    "doc_type": "invoice",
    "image_uri": "s3://.../gold_0001.png",
    "fields": [{"name": "合計金額", "value": "7003", "critical": true}, ...]
  }
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

from newfan_golden.metrics import GoldField


class GoldenFormatError(ValueError):
    """JSONL の形が契約に合わない。行番号を添えて投げる（どの行が悪いか分からないと直せない）。"""


def _parse_line(raw: str, lineno: int) -> dict[str, Any]:
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GoldenFormatError(f"{lineno} 行目: JSON として不正: {exc}") from exc
    if not isinstance(obj, dict):
        raise GoldenFormatError(f"{lineno} 行目: オブジェクトではありません")
    for key in ("document_id", "fields"):
        if key not in obj:
            raise GoldenFormatError(f"{lineno} 行目: 必須キー {key!r} がありません")
    if not isinstance(obj["fields"], list):
        raise GoldenFormatError(f"{lineno} 行目: fields は配列である必要があります")
    return obj


@dataclass(frozen=True)
class GoldenDoc:
    document_id: str
    fields: list[GoldField]
    doc_type: Optional[str] = None
    image_uri: Optional[str] = None
    # 抽出に使うスキーマを文書ごとに固定する。extraction_runs.schema_id が無いと
    # load_context は空スキーマを返し、KIE は 1 項目も抽出しない（Recall 0 になる）。
    schema_id: Optional[str] = None


def load_jsonl(path: Path) -> list[GoldenDoc]:
    docs: list[GoldenDoc] = []
    seen: set[str] = set()
    for lineno, raw in enumerate(_iter_lines(path), start=1):
        obj = _parse_line(raw, lineno)
        doc_id = str(obj["document_id"])
        if doc_id in seen:
            # 重複すると同じ文書を二重に数えて指標が歪む
            raise GoldenFormatError(f"{lineno} 行目: document_id {doc_id!r} が重複しています")
        seen.add(doc_id)
        docs.append(
            GoldenDoc(
                document_id=doc_id,
                doc_type=obj.get("doc_type"),
                image_uri=obj.get("image_uri"),
                schema_id=obj.get("schema_id"),
                fields=[
                    GoldField(
                        name=str(f["name"]),
                        value=f.get("value"),
                        critical=bool(f.get("critical", False)),
                    )
                    for f in obj["fields"]
                ],
            )
        )
    if not docs:
        raise GoldenFormatError(f"{path} にドキュメントがありません")
    return docs


def _iter_lines(path: Path) -> Iterator[str]:
    with path.open(encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if raw and not raw.startswith("//"):  # 空行と行コメントは飛ばす
                yield raw
