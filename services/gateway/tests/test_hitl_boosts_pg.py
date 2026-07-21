"""PgRepository.list_hitl_boosts × 実 PostgreSQL（§16 P5）。

waiting は workflow_runs の独立列ではなく state JSONB 内のキー
（runner の workflow_store._update が state || '{"waiting": ...}' で書く）。
初版が存在しない waiting 列を参照して /review/queue が Pg で常に 500 になる
バグをレビューで検出したため、実 DDL に対して SQL を固定する。

DATABASE_URL_TEST が設定されている時だけ動く。
"""

from __future__ import annotations

import os
import uuid

import pytest

pytest.importorskip("sqlalchemy")

_DSN = os.environ.get("DATABASE_URL_TEST")
pytestmark = pytest.mark.skipif(not _DSN, reason="DATABASE_URL_TEST 未設定（実 DB が要る）")


def test_list_hitl_boostsはstate内のwaitingから読む() -> None:
    import json

    from sqlalchemy import create_engine, text

    from newfan_gateway.db import PgRepository

    tenant = f"ten_boost_{uuid.uuid4().hex[:8]}"
    wf_id = f"workflow_{uuid.uuid4().hex[:12]}"
    owner = create_engine(_DSN, future=True)  # type: ignore[arg-type]
    with owner.begin() as c:
        c.execute(text("INSERT INTO tenants (id,name) VALUES (:t,'x')"), {"t": tenant})
        c.execute(
            text("INSERT INTO workflows (id,tenant_id,name,graph_json) VALUES (:w,:t,'wf','{}')"),
            {"w": wf_id, "t": tenant},
        )
        doc_a, doc_b = f"doc_a_{tenant}", f"doc_b_{tenant}"
        for run_id, doc, status, waiting in [
            ("wfrun_b1", doc_a, "waiting_hitl",
             {"kind": "await_hitl", "priority_boost": 25}),
            ("wfrun_b2", doc_a, "waiting_hitl", {"kind": "await_hitl"}),  # boost なし
            ("wfrun_b3", doc_b, "succeeded",
             {"kind": "await_hitl", "priority_boost": 99}),  # 終端済みは対象外
        ]:
            c.execute(
                text(
                    "INSERT INTO workflow_runs (id,tenant_id,workflow_id,workflow_version,"
                    " trigger,document_id,state,status)"
                    " VALUES (:r,:t,:w,1,'{}',:d,CAST(:s AS jsonb),:st)"
                ),
                {"r": f"{run_id}_{tenant}", "t": tenant, "w": wf_id, "d": doc,
                 "s": json.dumps({"waiting": waiting}), "st": status},
            )
    try:
        repo = PgRepository(_DSN)  # type: ignore[arg-type]
        # テストはスーパーユーザー接続で RLS が効かないため、他テナント行の混入は
        # 無視して自テナント分だけを見る（本番は RLS でテナント限定される）
        boosts = repo.list_hitl_boosts(tenant)
        assert boosts.get(doc_a) == 25  # boost あり(25)と無し(0)の MAX
        assert doc_b not in boosts  # 終端済みは加点対象外
    finally:
        with owner.begin() as c:
            c.execute(text("DELETE FROM workflow_runs WHERE tenant_id=:t"), {"t": tenant})
            c.execute(text("DELETE FROM workflows WHERE tenant_id=:t"), {"t": tenant})
            c.execute(text("DELETE FROM tenants WHERE id=:t"), {"t": tenant})
