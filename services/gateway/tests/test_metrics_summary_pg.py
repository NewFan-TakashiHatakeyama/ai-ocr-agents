"""SCR-04 KPI（フィールド精度 / LLM コスト）× 実 PostgreSQL（§12.1）。

- フィールド精度 = 確定 Run（直近 30 日）の無修正フィールド率
- LLM コスト = extraction_runs.metrics に永続化した実測トークン×単価の合計
- 計測データが無い場合は None（ダッシュボードは「—」のまま）

DATABASE_URL_TEST が設定されている時だけ動く。
"""

from __future__ import annotations

import os
import uuid

import pytest

pytest.importorskip("sqlalchemy")

_DSN = os.environ.get("DATABASE_URL_TEST")
pytestmark = pytest.mark.skipif(not _DSN, reason="DATABASE_URL_TEST 未設定（実 DB が要る）")


def test_metrics_summaryは精度とコストを実データから算出する() -> None:
    import json

    from sqlalchemy import create_engine, text

    from newfan_gateway.db import PgAdminRepository

    tenant = f"ten_kpi_{uuid.uuid4().hex[:8]}"
    owner = create_engine(_DSN, future=True)  # type: ignore[arg-type]
    with owner.begin() as c:
        c.execute(text("INSERT INTO tenants (id,name) VALUES (:t,'x')"), {"t": tenant})
        c.execute(
            text(
                "INSERT INTO documents (id,tenant_id,storage_uri,mime_type,status)"
                " VALUES ('doc_kpi',:t,'s3://x','image/png','confirmed')"
            ),
            {"t": tenant},
        )
        # 確定 Run 1 件（コスト計測あり）+ 未計測 Run 1 件
        c.execute(
            text(
                "INSERT INTO extraction_runs (id,tenant_id,document_id,status,"
                " engine_versions,metrics) VALUES"
                " ('run_kpi1',:t,'doc_kpi','confirmed','{}', CAST(:m AS jsonb)),"
                " ('run_kpi2',:t,'doc_kpi','confirmed','{}', '{}')"
            ),
            {"t": tenant, "m": json.dumps(
                {"llm_input_tokens": 1200, "llm_output_tokens": 340, "llm_cost_jpy": 1.25}
            )},
        )
        # フィールド 4 本中 1 本に修正あり → 精度 0.75
        for i in range(4):
            c.execute(
                text(
                    "INSERT INTO extraction_fields (id,tenant_id,run_id,field_name,"
                    " correction) VALUES (:i,:t,'run_kpi1',:n, CAST(:c AS jsonb))"
                ),
                {
                    "i": f"ef_kpi{i}_{tenant}", "t": tenant, "n": f"f{i}",
                    "c": json.dumps({"corrected_value": "x"}) if i == 0 else None,
                },
            )
    try:
        repo = PgAdminRepository(_DSN)  # type: ignore[arg-type]
        m = repo.metrics_summary(tenant)
        assert m.field_accuracy_sampled == pytest.approx(0.75)
        assert m.llm_cost_jpy_total == pytest.approx(1.25)
    finally:
        with owner.begin() as c:
            for sql in (
                "DELETE FROM extraction_fields WHERE tenant_id=:t",
                "DELETE FROM extraction_runs WHERE tenant_id=:t",
                "DELETE FROM documents WHERE tenant_id=:t",
                "DELETE FROM tenants WHERE id=:t",
            ):
                c.execute(text(sql), {"t": tenant})


def test_計測データが無ければNoneのまま() -> None:
    from sqlalchemy import create_engine, text

    from newfan_gateway.db import PgAdminRepository

    tenant = f"ten_kpi0_{uuid.uuid4().hex[:8]}"
    owner = create_engine(_DSN, future=True)  # type: ignore[arg-type]
    with owner.begin() as c:
        c.execute(text("INSERT INTO tenants (id,name) VALUES (:t,'x')"), {"t": tenant})
    try:
        m = PgAdminRepository(_DSN).metrics_summary(tenant)  # type: ignore[arg-type]
        assert m.field_accuracy_sampled is None
        assert m.llm_cost_jpy_total is None
    finally:
        with owner.begin() as c:
            c.execute(text("DELETE FROM tenants WHERE id=:t"), {"t": tenant})


def test_add_run_metricsは加算で積み上がる() -> None:
    from sqlalchemy import create_engine, text

    from newfan_orchestrator.pg_persistence import PgContextStore

    tenant = f"ten_arm_{uuid.uuid4().hex[:8]}"
    owner = create_engine(_DSN, future=True)  # type: ignore[arg-type]
    with owner.begin() as c:
        c.execute(text("INSERT INTO tenants (id,name) VALUES (:t,'x')"), {"t": tenant})
        c.execute(
            text(
                "INSERT INTO documents (id,tenant_id,storage_uri,mime_type,status)"
                " VALUES ('doc_arm',:t,'s3://x','image/png','uploaded')"
            ),
            {"t": tenant},
        )
        c.execute(
            text(
                "INSERT INTO extraction_runs (id,tenant_id,document_id,engine_versions,"
                " metrics) VALUES ('run_arm',:t,'doc_arm','{}', CAST(:m AS jsonb))"
            ),
            {"t": tenant, "m": '{"fallback_pages": []}'},
        )
    try:
        store = PgContextStore(_DSN)  # type: ignore[arg-type]
        store.add_run_metrics(tenant, "run_arm", {"llm_input_tokens": 100, "llm_cost_jpy": 0.5})
        store.add_run_metrics(tenant, "run_arm", {"llm_input_tokens": 50, "llm_cost_jpy": 0.25})
        with owner.begin() as c:
            m = c.execute(
                text("SELECT metrics FROM extraction_runs WHERE id='run_arm'")
            ).scalar_one()
        assert float(m["llm_input_tokens"]) == 150
        assert float(m["llm_cost_jpy"]) == pytest.approx(0.75)
        assert m["fallback_pages"] == []  # 既存キーは保持（上書きしない）
    finally:
        with owner.begin() as c:
            c.execute(text("DELETE FROM extraction_runs WHERE tenant_id=:t"), {"t": tenant})
            c.execute(text("DELETE FROM documents WHERE tenant_id=:t"), {"t": tenant})
            c.execute(text("DELETE FROM tenants WHERE id=:t"), {"t": tenant})
