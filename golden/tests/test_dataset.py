"""JSONL の読み込み契約。壊れた行を黙って飛ばすと指標が静かに嘘になるため、
「必ず例外にする」ことを固定する。"""

from __future__ import annotations

from pathlib import Path

import pytest

from newfan_golden.dataset import GoldenFormatError, load_jsonl

VALID = (
    '{"document_id": "gold_0001", "doc_type": "invoice", '
    '"fields": [{"name": "合計金額", "value": "7003", "critical": true}]}'
)


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "gold.jsonl"
    p.write_text(body, encoding="utf-8")
    return p


def test_正常な行を読める(tmp_path: Path) -> None:
    docs = load_jsonl(_write(tmp_path, VALID + "\n"))
    assert len(docs) == 1
    assert docs[0].document_id == "gold_0001"
    assert docs[0].doc_type == "invoice"
    assert docs[0].fields[0].critical is True


def test_空行と行コメントは飛ばす(tmp_path: Path) -> None:
    docs = load_jsonl(_write(tmp_path, f"// 2026-07 版\n\n{VALID}\n\n"))
    assert len(docs) == 1


def test_壊れたJSONは行番号つきで落ちる(tmp_path: Path) -> None:
    with pytest.raises(GoldenFormatError, match="2 行目"):
        load_jsonl(_write(tmp_path, VALID + "\n{壊れている\n"))


def test_必須キー欠落で落ちる(tmp_path: Path) -> None:
    with pytest.raises(GoldenFormatError, match="fields"):
        load_jsonl(_write(tmp_path, '{"document_id": "x"}\n'))


def test_document_idの重複で落ちる(tmp_path: Path) -> None:
    # 重複を許すと同じ文書を二重に数えて指標が歪む
    with pytest.raises(GoldenFormatError, match="重複"):
        load_jsonl(_write(tmp_path, VALID + "\n" + VALID + "\n"))


def test_空ファイルで落ちる(tmp_path: Path) -> None:
    # 0 件を通すと「全項目正解」に見えてゲートが素通りする
    with pytest.raises(GoldenFormatError):
        load_jsonl(_write(tmp_path, "\n// なにもない\n"))
