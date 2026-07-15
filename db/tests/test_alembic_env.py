"""alembic env.py の DATABASE_URL 取り込み（§7）。

RDS の生成パスワードは記号を含み URL エンコードされる（例: %2A）。alembic の
set_main_option は configparser 経由で % を補間構文として解釈するため、素通しすると
`ValueError: invalid interpolation syntax` で migrate が落ちる（実 AWS で検出）。
"""

from __future__ import annotations

import configparser

import pytest

# 実際の RDS が生成したパスワードを URL エンコードした形（実 apply の値の形）
ENCODED_URL = (
    "postgresql+psycopg://newfan:eeIz%29%2A%23S%24U%2AUFMhyJ23M%2B%23sTlpq%7BW%2Avm"
    "@ai-ocr-production.c9046q8m49xy.ap-northeast-1.rds.amazonaws.com:5432/newfan"
)


def _set_via_configparser(value: str) -> str:
    """alembic Config.set_main_option と同じ経路（configparser）を通す。"""
    cp = configparser.ConfigParser()
    cp.add_section("alembic")
    cp.set("alembic", "sqlalchemy.url", value)
    return cp.get("alembic", "sqlalchemy.url")


def test_raw_encoded_url_breaks_configparser() -> None:
    """エスケープしないと落ちること（この前提が変わったら env.py の対処も見直す）。"""
    with pytest.raises(ValueError):
        _set_via_configparser(ENCODED_URL)


def test_escaped_url_roundtrips() -> None:
    """% を %% にすれば設定でき、読み出すと元の URL に戻る。"""
    assert _set_via_configparser(ENCODED_URL.replace("%", "%%")) == ENCODED_URL


def test_url_without_percent_is_unaffected() -> None:
    plain = "postgresql+psycopg://u:p@localhost:5432/db"
    assert _set_via_configparser(plain.replace("%", "%%")) == plain
