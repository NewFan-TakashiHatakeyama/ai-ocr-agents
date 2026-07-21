"""PgDbWriter × 実 PostgreSQL（DD-12 / §16 P6）。

dbsink が生成した SQL が実 PG で意図どおり動くことを固定する:
- insert: nf_write_key UNIQUE + ON CONFLICT DO NOTHING で再実行しても増えない
- upsert: 同一キーの再書込みは UPDATE になる

DATABASE_URL_TEST が設定されている時だけ動く。
"""

from __future__ import annotations

import os
import uuid

import pytest

pytest.importorskip("psycopg")

_DSN = os.environ.get("DATABASE_URL_TEST")
pytestmark = pytest.mark.skipif(not _DSN, reason="DATABASE_URL_TEST 未設定（実 DB が要る）")


@pytest.fixture()
def table():  # noqa: ANN201
    import psycopg

    name = f"sink_t_{uuid.uuid4().hex[:8]}"
    plain = _DSN.replace("+psycopg", "")  # type: ignore[union-attr]
    with psycopg.connect(plain, autocommit=True) as conn:
        conn.execute(
            f"CREATE TABLE {name} ("
            " invoice_no TEXT UNIQUE, amount TEXT, nf_write_key TEXT UNIQUE)"
        )
    yield plain, name
    with psycopg.connect(plain, autocommit=True) as conn:
        conn.execute(f"DROP TABLE IF EXISTS {name}")


def _count(plain: str, name: str) -> list[tuple]:
    import psycopg

    with psycopg.connect(plain) as conn:
        return conn.execute(
            f"SELECT invoice_no, amount FROM {name} ORDER BY invoice_no"
        ).fetchall()


def test_insertの再実行はnf_write_keyで冪等(table) -> None:  # noqa: ANN001
    from newfan_workflow.dbsink import build_db_write_sql
    from newfan_workflow.models import DbWriteConfig

    from newfan_orchestrator.workflow_sinks import PgDbWriter

    plain, name = table
    cfg = DbWriteConfig(connection_id="c", table=name, mode="insert")
    sql = build_db_write_sql(cfg, ["invoice_no", "amount", "nf_write_key"])
    rows = [{"invoice_no": "GS0001", "amount": "7003", "nf_write_key": "wfrun_x:d1:0"}]

    writer = PgDbWriter()
    assert writer.write(plain, sql, rows) == 1
    # 再実行（resume の replay / retry）。台帳キーの一意制約で黙って捨てられる
    writer.write(plain, sql, rows)
    assert _count(plain, name) == [("GS0001", "7003")]


def test_insertの業務キー重複は黙殺されず可視のエラーになる(table) -> None:  # noqa: ANN001
    """target 無し ON CONFLICT の黙殺バグの回帰（レビューで実 PG 再現・修正）。

    別ドキュメント（別台帳キー）が同じ invoice_no を持つ場合、行を黙って捨てるのではなく
    UniqueViolation で失敗し、run が failed になって運用者に見える。
    """
    import psycopg

    from newfan_workflow.dbsink import build_db_write_sql
    from newfan_workflow.models import DbWriteConfig

    from newfan_orchestrator.workflow_sinks import PgDbWriter

    plain, name = table
    cfg = DbWriteConfig(connection_id="c", table=name, mode="insert")
    sql = build_db_write_sql(cfg, ["invoice_no", "amount", "nf_write_key"])
    writer = PgDbWriter()
    writer.write(plain, sql, [{"invoice_no": "GS0001", "amount": "1", "nf_write_key": "r1:d1:0"}])
    with pytest.raises(psycopg.errors.UniqueViolation):
        writer.write(
            plain, sql, [{"invoice_no": "GS0001", "amount": "2", "nf_write_key": "r2:d1:0"}]
        )
    assert _count(plain, name) == [("GS0001", "1")]


def test_upsertは同一キーを更新する(table) -> None:  # noqa: ANN001
    from newfan_workflow.dbsink import build_db_write_sql
    from newfan_workflow.models import DbWriteConfig

    from newfan_orchestrator.workflow_sinks import PgDbWriter

    plain, name = table
    cfg = DbWriteConfig(connection_id="c", table=name, mode="upsert", keys=["invoice_no"])
    sql = build_db_write_sql(cfg, ["invoice_no", "amount"])

    writer = PgDbWriter()
    writer.write(plain, sql, [{"invoice_no": "GS0001", "amount": "7003"}])
    writer.write(plain, sql, [{"invoice_no": "GS0001", "amount": "9999"}])  # 修正後の再送
    writer.write(plain, sql, [{"invoice_no": "GS0002", "amount": "1"}])
    assert _count(plain, name) == [("GS0001", "9999"), ("GS0002", "1")]
