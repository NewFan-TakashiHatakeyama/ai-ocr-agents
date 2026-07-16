"""RLS のテナント分離を実 PostgreSQL で検証する（§7.3）。

実 RDS で計測したところ、app.tenant_id を設定しても他テナントの行が見えていた。
PostgreSQL の**テーブル所有者は RLS をバイパスする**のに、アプリがマスタ（=所有者）で
繋いでいたため。ポリシーも ENABLE も書かれていたので、コードを読むだけでは気づけない。

ここでは所有者ではないロールを作って接続し、実際に分離が効くことを確かめる。
ローカル compose の newfan は superuser（無条件バイパス）なので、所有者のまま測っても
何も検証したことにならない。

DATABASE_URL_TEST が設定されている時だけ動く（CI は workflow の postgres service）。
"""

from __future__ import annotations

import os
import uuid

import pytest

pytest.importorskip("sqlalchemy", reason="実 DB 検証は runtime 依存")
pytest.importorskip("psycopg", reason="実 DB 検証は runtime 依存")

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.exc import ProgrammingError  # noqa: E402

_DSN = os.environ.get("DATABASE_URL_TEST")
pytestmark = pytest.mark.skipif(not _DSN, reason="DATABASE_URL_TEST 未設定（実 DB が要る）")

TEN_A = "ten_rls_a"
TEN_B = "ten_rls_b"
APP_ROLE = "rls_test_app"
APP_PW = "rls_test_pw"  # noqa: S105 - ローカル/CI のテスト専用ロール


@pytest.fixture(scope="module")
def owner():
    return create_engine(_DSN, future=True)  # type: ignore[arg-type]


@pytest.fixture(scope="module")
def app_engine(owner):
    """所有者でないロールを作り、そのロールでの接続を返す。"""
    with owner.begin() as c:
        c.execute(
            text(
                f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{APP_ROLE}')"
                f" THEN CREATE ROLE {APP_ROLE} LOGIN PASSWORD '{APP_PW}'; END IF; END $$;"
            )
        )
        # 念のため。BYPASSRLS/SUPERUSER を持っていると分離が効かず、テストが無意味になる。
        c.execute(text(f"ALTER ROLE {APP_ROLE} NOBYPASSRLS NOSUPERUSER"))
        c.execute(text("GRANT USAGE ON SCHEMA public TO " + APP_ROLE))
        for t in ("tenants", "documents"):
            c.execute(text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {t} TO {APP_ROLE}"))

    url = _DSN.split("://", 1)[1].split("@", 1)[1]  # type: ignore[union-attr]
    eng = create_engine(f"postgresql+psycopg://{APP_ROLE}:{APP_PW}@{url}", future=True)
    yield eng
    eng.dispose()


@pytest.fixture
def seeded(owner):
    """2 テナントの documents を用意する（所有者で投入）。"""
    ids = {}
    with owner.begin() as c:
        for t in (TEN_A, TEN_B):
            c.execute(
                text("INSERT INTO tenants (id, name) VALUES (:i,'rls test')"
                     " ON CONFLICT (id) DO NOTHING"),
                {"i": t},
            )
            did = f"doc_{uuid.uuid4().hex[:12]}"
            ids[t] = did
            c.execute(
                text("INSERT INTO documents (id, tenant_id, storage_uri, mime_type,"
                     " page_count, status) VALUES (:i,:t,'s3://x','image/png',1,'uploaded')"),
                {"i": did, "t": t},
            )
    yield ids
    with owner.begin() as c:
        c.execute(text("DELETE FROM documents WHERE tenant_id IN (:a,:b)"), {"a": TEN_A, "b": TEN_B})
        c.execute(text("DELETE FROM tenants WHERE id IN (:a,:b)"), {"a": TEN_A, "b": TEN_B})


def test_アプリロールは自テナントの行しか見えない(app_engine, seeded) -> None:
    with app_engine.begin() as c:
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TEN_A})
        rows = [
            r[0]
            for r in c.execute(
                text("SELECT id FROM documents WHERE id IN (:a,:b)"),
                {"a": seeded[TEN_A], "b": seeded[TEN_B]},
            )
        ]
    assert rows == [seeded[TEN_A]], "他テナントの行が見えている＝RLS がバイパスされている"


def test_app_tenant_id未設定なら何も見えない(app_engine, seeded) -> None:
    # ポリシーは tenant_id = current_setting('app.tenant_id', true)。未設定だと NULL 比較で
    # 0 件になる。「設定し忘れたら全部見える」ではないことを固定する。
    with app_engine.begin() as c:
        rows = list(c.execute(text("SELECT id FROM documents")))
    assert rows == []


def test_他テナントの行は更新できない(app_engine, seeded) -> None:
    with app_engine.begin() as c:
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TEN_A})
        res = c.execute(
            text("UPDATE documents SET status='confirmed' WHERE id = :i"),
            {"i": seeded[TEN_B]},
        )
    assert res.rowcount == 0


def test_他テナントを騙る行は挿入できない(app_engine, seeded) -> None:
    # USING しか書いていないポリシーは INSERT の WITH CHECK にも使われる。
    # ここが効かないと、テナントを詐称した行を書き込めてしまう。
    with pytest.raises(ProgrammingError):
        with app_engine.begin() as c:
            c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TEN_A})
            c.execute(
                text("INSERT INTO documents (id, tenant_id, storage_uri, mime_type,"
                     " page_count, status) VALUES ('doc_spoof',:t,'s3://x','image/png',1,"
                     " 'uploaded')"),
                {"t": TEN_B},
            )


def test_アプリロールは所有者でもバイパスでもない(app_engine) -> None:
    # このテストの前提そのものを固定する。ロールが所有者や BYPASSRLS になった瞬間、
    # 上の分離テストは全部素通りして「緑だが何も検証していない」状態になる。
    with app_engine.begin() as c:
        row = c.execute(
            text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        ).first()
        owner_name = c.execute(
            text("SELECT tableowner FROM pg_tables WHERE tablename='documents'")
        ).scalar()
        me = c.execute(text("SELECT current_user")).scalar()
    assert row == (False, False), "テスト用ロールが superuser/BYPASSRLS になっている"
    assert owner_name != me, "テスト用ロールがテーブル所有者になっている"


def test_業務テーブルは_force_rls_になっている(owner) -> None:
    # FORCE が無いと所有者バイパスが復活する。マイグレーションの付け忘れを機械で検知する。
    with owner.begin() as c:
        rows = c.execute(
            text(
                "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class"
                " WHERE relname IN ('documents','extraction_fields','correction_logs',"
                " 'tenant_memories','tenant_rules','jobs','audit_logs')"
            )
        ).all()
    assert rows, "対象テーブルが見つからない（migration 未適用？）"
    not_forced = [r[0] for r in rows if not (r[1] and r[2])]
    assert not_forced == [], f"RLS が ENABLE+FORCE でないテーブル: {not_forced}"
