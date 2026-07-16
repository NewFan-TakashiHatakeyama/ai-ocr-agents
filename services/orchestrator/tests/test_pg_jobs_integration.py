"""PgContextStore.set_job_status を実 PostgreSQL に対して検証する（§7 / §9）。

ワーカーは jobs を一切更新しておらず、GET /jobs/{id} が成功後も queued を返し続けていた
（§6.3 は polling 契約なのでクライアントは永久に待つ）。InMemory だけのテストでは
jobs テーブルの CHECK 制約も列名も検証できないため、実 DB に当てる。

DATABASE_URL_TEST が設定されている時だけ動く（CI は workflow の postgres service を指す）。
"""

from __future__ import annotations

import os
import uuid

import pytest

pytest.importorskip("sqlalchemy", reason="PgContextStore は runtime 依存")
pytest.importorskip("psycopg", reason="PgContextStore は runtime 依存")

_DSN = os.environ.get("DATABASE_URL_TEST")
pytestmark = pytest.mark.skipif(not _DSN, reason="DATABASE_URL_TEST 未設定（実 DB が要る）")

TENANT = "ten_jobs_test"


@pytest.fixture
def store():
    from newfan_orchestrator.pg_persistence import PgContextStore

    return PgContextStore(_DSN)  # type: ignore[arg-type]


@pytest.fixture
def job_id(store):
    from sqlalchemy import text

    jid = f"job_{uuid.uuid4().hex[:12]}"
    with store._engine.begin() as c:  # noqa: SLF001 - テスト用の前提データ投入
        c.execute(
            text("INSERT INTO tenants (id, name) VALUES (:i,'test') ON CONFLICT (id) DO NOTHING"),
            {"i": TENANT},
        )
        c.execute(
            text(
                "INSERT INTO jobs (id, tenant_id, kind, ref_id) VALUES (:i,:t,'extract',:r)"
            ),
            {"i": jid, "t": TENANT, "r": f"run_{uuid.uuid4().hex[:12]}"},
        )
    yield jid
    with store._engine.begin() as c:  # noqa: SLF001
        c.execute(text("DELETE FROM jobs WHERE id = :i"), {"i": jid})


def _row(store, job_id: str):
    from sqlalchemy import text

    with store._engine.begin() as c:  # noqa: SLF001
        return c.execute(
            text(
                "SELECT status, attempt, error_code, started_at, finished_at "
                "FROM jobs WHERE id = :i"
            ),
            {"i": job_id},
        ).first()


def test_running_から_succeeded_まで遷移する(store, job_id) -> None:
    assert _row(store, job_id)[0] == "queued"

    store.set_job_status(TENANT, job_id, "running")
    status, attempt, _, started_at, finished_at = _row(store, job_id)
    assert (status, attempt) == ("running", 1)
    assert started_at is not None
    assert finished_at is None

    store.set_job_status(TENANT, job_id, "succeeded")
    status, attempt, err, _, finished_at = _row(store, job_id)
    assert (status, err) == ("succeeded", None)
    assert attempt == 1  # 成功で attempt は増えない
    assert finished_at is not None


def test_failed_は_error_code_を残す(store, job_id) -> None:
    store.set_job_status(TENANT, job_id, "running")
    store.set_job_status(TENANT, job_id, "failed", error_code="E9001")
    status, _, err, _, finished_at = _row(store, job_id)
    assert (status, err) == ("failed", "E9001")
    assert finished_at is not None


def test_再配信で_attempt_が増える(store, job_id) -> None:
    # §9 の再配信。何回試したかが残らないと dead 判定も障害調査もできない。
    store.set_job_status(TENANT, job_id, "running")
    store.set_job_status(TENANT, job_id, "failed", error_code="E9001")
    store.set_job_status(TENANT, job_id, "running")
    assert _row(store, job_id)[1] == 2


def test_他テナントのジョブは更新できない(store, job_id) -> None:
    store.set_job_status("ten_other", job_id, "succeeded")
    assert _row(store, job_id)[0] == "queued"


def test_DDLのCHECK制約に無いstatusは弾かれる(store, job_id) -> None:
    # 'done' や 'completed' のような綴りを実装側が使い始めても DB が拒否する。
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        store.set_job_status(TENANT, job_id, "done")
