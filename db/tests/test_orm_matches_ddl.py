"""ORM の列定義が §7 DDL と一致することを検証する。

correction_logs は ORM だけが note を持ち、DDL にしかない doc_type/supplier_key/context を
欠いていたため、修正の保存が本番で
  psycopg.errors.UndefinedColumn: column "note" of relation "correction_logs" does not exist
になり、学習ループ（DD-06/DD-07）の入口が丸ごと壊れていた（実 AWS で検出）。
InMemory リポジトリのテストしか無かったため誰も踏まなかった。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy", reason="PgRepository は runtime 依存")

from newfan_gateway.db import Base  # noqa: E402

_DDL = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0001_initial_schema.py"


def _ddl_columns(table: str) -> set[str]:
    sql = _DDL.read_text(encoding="utf-8")
    m = re.search(rf"CREATE TABLE {table} \((.*?)\n\);", sql, re.S)
    assert m, f"{table} の CREATE TABLE が見つかりません"

    body = re.sub(r"--[^\n]*", "", m.group(1))  # 行コメント除去
    # 括弧の外側のカンマで分割する（1 行に複数列を書いている箇所や
    # DEFAULT '{}' / NUMERIC(12,2) があるため、行単位・単純 split では取りこぼす）
    parts, depth, cur = [], 0, ""
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)

    cols: set[str] = set()
    for p in parts:
        p = p.strip()
        if not p or p.upper().startswith(("CONSTRAINT", "PRIMARY KEY", "UNIQUE", "FOREIGN", "CHECK")):
            continue
        name = p.split()[0]
        if name.isidentifier():
            cols.add(name)
    return cols


@pytest.mark.parametrize("table", sorted(Base.metadata.tables))
def test_orm_columns_exist_in_ddl(table: str) -> None:
    """ORM の全列が DDL に存在すること（存在しない列に INSERT すると本番で 500）。"""
    ddl = _ddl_columns(table)
    orm = {c.name for c in Base.metadata.tables[table].columns}
    missing = orm - ddl
    assert not missing, f"{table}: ORM にあるが DDL に無い列 {sorted(missing)}"


def test_correction_logs_carries_learning_keys() -> None:
    """学習ループが使う列を ORM が持つこと。

    doc_type/supplier_key は memory の検索キー、context は embedding 入力（§5.8）。
    idx_corrections_pattern も (tenant_id, doc_type, field_name) 前提。
    """
    orm = {c.name for c in Base.metadata.tables["correction_logs"].columns}
    assert {"doc_type", "supplier_key", "context"} <= orm
