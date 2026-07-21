"""dbsink（DD-12 の SQL 生成）。プレビュー = 実 SQL の単一実装をここで固定する。"""

from __future__ import annotations

import pytest

from newfan_workflow.dbsink import (
    LEDGER_COLUMN,
    MAX_ROWS_PER_RUN,
    DbSinkError,
    build_db_write_sql,
    build_dsn,
    check_allowed_table,
    check_row_limit,
)
from newfan_workflow.models import DbWriteConfig


def _cfg(**kw) -> DbWriteConfig:
    base = {"connection_id": "con_pg", "table": "erp.invoices", "mode": "insert"}
    return DbWriteConfig(**{**base, **kw})


def test_insertは台帳キー限定のON_CONFLICTになる() -> None:
    # target 無しの ON CONFLICT は業務キー重複まで黙殺する（無音データ欠落）ため、
    # 必ず台帳キーに限定する（レビューで実 PG 再現・修正）
    sql = build_db_write_sql(_cfg(), ["invoice_no", "amount", LEDGER_COLUMN])
    assert sql == (
        'INSERT INTO "erp"."invoices" ("invoice_no", "amount", "nf_write_key")'
        " VALUES (%(invoice_no)s, %(amount)s, %(nf_write_key)s)"
        ' ON CONFLICT ("nf_write_key") DO NOTHING'
    )


def test_insertは台帳キー列が無いと拒否() -> None:
    with pytest.raises(DbSinkError, match="台帳キー"):
        build_db_write_sql(_cfg(), ["invoice_no", "amount"])


def test_upsertはキー列でDO_UPDATE() -> None:
    cfg = _cfg(mode="upsert", keys=["invoice_no", "issuer_name"])
    sql = build_db_write_sql(cfg, ["invoice_no", "issuer_name", "amount"])
    assert sql == (
        'INSERT INTO "erp"."invoices" ("invoice_no", "issuer_name", "amount")'
        " VALUES (%(invoice_no)s, %(issuer_name)s, %(amount)s)"
        ' ON CONFLICT ("invoice_no", "issuer_name") DO UPDATE SET'
        ' "amount" = EXCLUDED."amount"'
    )


def test_upsertで全列がキーならDO_NOTHING() -> None:
    cfg = _cfg(mode="upsert", keys=["invoice_no"])
    sql = build_db_write_sql(cfg, ["invoice_no"])
    assert sql.endswith('ON CONFLICT ("invoice_no") DO NOTHING')


def test_upsertのキー列が書込み列に無いと拒否() -> None:
    cfg = _cfg(mode="upsert", keys=["invoice_no"])
    with pytest.raises(DbSinkError, match="キー列"):
        build_db_write_sql(cfg, ["amount"])


def test_不正な識別子は拒否される() -> None:
    # モデル検証をすり抜けても（graph_json 直改変等）ビルダで再検証して落とす
    with pytest.raises(DbSinkError, match="識別子"):
        build_db_write_sql(_cfg(), ['amount"; DROP TABLE x; --'])


def test_列の重複と空列は拒否() -> None:
    with pytest.raises(DbSinkError, match="重複"):
        build_db_write_sql(_cfg(), ["a", "a"])
    with pytest.raises(DbSinkError, match="列がありません"):
        build_db_write_sql(_cfg(), [])


def test_allowed_tablesは完全一致() -> None:
    check_allowed_table("erp.invoices", ["erp.invoices"])
    for allowed in ([], None, ["erp.invoices_v2"], ["invoices"], ["ERP.invoices"]):
        with pytest.raises(DbSinkError, match="allowed_tables"):
            check_allowed_table("erp.invoices", allowed)


def test_行数上限は1000() -> None:
    check_row_limit(MAX_ROWS_PER_RUN)
    with pytest.raises(DbSinkError, match="上限"):
        check_row_limit(MAX_ROWS_PER_RUN + 1)


def test_build_dsnは完全DSN秘密を優先しconfig秘密を拒否() -> None:
    assert build_dsn({}, "postgresql://u:p@h:5/db") == "postgresql://u:p@h:5/db"
    assert build_dsn({}, "postgresql+psycopg://u:p@h/db") == "postgresql://u:p@h/db"
    assert (
        build_dsn({"host": "h", "dbname": "db", "user": "u", "port": 5433}, "pw")
        == "postgresql://u:pw@h:5433/db"
    )
    with pytest.raises(DbSinkError, match="秘密"):
        build_dsn({"password": "x", "host": "h", "dbname": "d", "user": "u"}, None)
    with pytest.raises(DbSinkError, match="host/dbname/user"):
        build_dsn({}, None)


def test_build_dsnは記号入りパスワードをpercent_encodeする() -> None:
    # Secrets Manager の自動生成パスワードは @ / % 等を普通に含む。生連結だと
    # libpq の URI 解釈が壊れる（p@ss → host 誤パース等。レビューで実測）
    dsn = build_dsn({"host": "h", "dbname": "db", "user": "u"}, "p@s/s%w:d")
    assert dsn == "postgresql://u:p%40s%2Fs%25w%3Ad@h:5432/db"
