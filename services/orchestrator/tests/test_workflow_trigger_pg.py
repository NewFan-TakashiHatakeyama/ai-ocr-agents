"""PgTriggerStore × 実 PostgreSQL（§16 P4）。

実 DDL に対して固定する:
- list_active_workflows は active のみ / get_s3_connection_bucket は type='s3' のみ
- register_ingested は claim + documents + pages + workflow_runs を 1 TX で入れ、
  二重 claim（SQS at-least-once の再配信）は [] を返して何も増やさない

DATABASE_URL_TEST が設定されている時だけ動く。
"""

from __future__ import annotations

import json
import os
import uuid

import pytest

pytest.importorskip("sqlalchemy")

_DSN = os.environ.get("DATABASE_URL_TEST")
pytestmark = pytest.mark.skipif(not _DSN, reason="DATABASE_URL_TEST 未設定（実 DB が要る）")

GRAPH = {
    "version": 1,
    "nodes": [
        {
            "id": "t1",
            "type": "source.s3_event",
            "config": {"connection_id": "con_s3x", "prefix": "invoices/"},
        }
    ],
    "edges": [],
}


@pytest.fixture()
def seeded():  # noqa: ANN201
    import json

    from sqlalchemy import create_engine, text

    tenant = f"ten_trig_{uuid.uuid4().hex[:8]}"
    wf_active = f"workflow_{uuid.uuid4().hex[:12]}"
    wf_draft = f"workflow_{uuid.uuid4().hex[:12]}"
    owner = create_engine(_DSN, future=True)  # type: ignore[arg-type]
    with owner.begin() as c:
        # 固定 ID の接続は、前回 run が途中で落ちた残骸と衝突し得るので先に消す
        c.execute(text("DELETE FROM connections WHERE id IN ('con_s3x','con_hookx')"))
        c.execute(text("INSERT INTO tenants (id,name) VALUES (:t,'x')"), {"t": tenant})
        c.execute(
            text(
                "INSERT INTO workflows (id,tenant_id,name,graph_json,status,version)"
                " VALUES (:w,:t,'wf',CAST(:g AS jsonb),'active',3),"
                "        (:w2,:t,'wf2',CAST(:g AS jsonb),'draft',1)"
            ),
            {"w": wf_active, "w2": wf_draft, "t": tenant, "g": json.dumps(GRAPH)},
        )
        c.execute(
            text(
                "INSERT INTO connections (id,tenant_id,type,name,config,status) VALUES"
                " ('con_s3x',:t,'s3','inbox','{\"bucket\": \"inbox-bkt\"}','tested'),"
                " ('con_hookx',:t,'webhook','hook','{}','tested')"
            ),
            {"t": tenant},
        )
    yield tenant, wf_active
    with owner.begin() as c:
        for sql in (
            "DELETE FROM workflow_runs WHERE tenant_id=:t",
            "DELETE FROM source_cursors WHERE tenant_id=:t",
            "DELETE FROM pages WHERE tenant_id=:t",
            "DELETE FROM documents WHERE tenant_id=:t",
            "DELETE FROM connections WHERE tenant_id=:t",
            "DELETE FROM workflows WHERE tenant_id=:t",
            "DELETE FROM tenants WHERE id=:t",
        ):
            c.execute(text(sql), {"t": tenant})


def _store():  # noqa: ANN202
    from newfan_orchestrator.workflow_store import PgTriggerStore

    return PgTriggerStore(_DSN)  # type: ignore[arg-type]


def test_activeのワークフローとs3接続だけが引ける(seeded) -> None:  # noqa: ANN001
    tenant, wf_active = seeded
    store = _store()
    wfs = store.list_active_workflows(tenant)
    assert [(w[0], w[1]) for w in wfs] == [(wf_active, 3)]
    assert wfs[0][2]["nodes"][0]["type"] == "source.s3_event"
    assert store.get_s3_connection_bucket(tenant, "con_s3x") == "inbox-bkt"
    # webhook 接続や他テナントの接続は s3 として引けない
    assert store.get_s3_connection_bucket(tenant, "con_hookx") is None
    assert store.get_s3_connection_bucket("ten_other", "con_s3x") is None


def test_register_ingestedは1TXで登録し再配信は空を返す(seeded) -> None:  # noqa: ANN001
    from sqlalchemy import create_engine, text

    from newfan_orchestrator.workflow_trigger import TriggerMatch

    tenant, wf_active = seeded
    store = _store()
    doc_id = f"doc_{uuid.uuid4().hex[:24]}"
    match = TriggerMatch(
        workflow_id=wf_active,
        workflow_version=3,
        graph_json=GRAPH,
        node_id="t1",
        connection_id="con_s3x",
    )
    key, etag = "ten_x/invoices/a.png", "etag-abc"
    assert store.already_claimed(tenant, "con_s3x", key, etag) is False

    run_ids = store.register_ingested(
        tenant,
        source_key=key,
        content_hash=etag,
        document={
            "id": doc_id,
            "storage_uri": "s3://main/x/original.png",
            "original_name": "a.png",
            "mime_type": "image/png",
            "page_count": 1,
            "external_ref": "s3://inbox-bkt/ten_x/invoices/a.png",
        },
        pages=[
            {"page_no": 1, "width": 10, "height": 20, "image_uri": "s3://main/x/p1.png",
             "preproc": {"deskew": 0.0}}
        ],
        matches=[match],
    )
    assert len(run_ids) == 1
    assert store.already_claimed(tenant, "con_s3x", key, etag) is True

    owner = create_engine(_DSN, future=True)  # type: ignore[arg-type]
    with owner.begin() as c:
        run = c.execute(
            text(
                "SELECT workflow_id, workflow_version, document_id, status,"
                " trigger->>'type', trigger->>'source_key', trigger->'graph_json'"
                " FROM workflow_runs WHERE id=:r"
            ),
            {"r": run_ids[0]},
        ).first()
        page = c.execute(
            text("SELECT id FROM pages WHERE document_id=:d"), {"d": doc_id}
        ).scalar()
    assert run is not None
    assert (run[0], run[1], run[2], run[3]) == (wf_active, 3, doc_id, "running")
    # runner は trigger.graph_json スナップショットで実行する（§11.1 版の固定）
    assert (run[4], run[5]) == ("s3_event", key)
    assert run[6] == GRAPH
    assert page == f"{doc_id}:1"

    # SQS at-least-once: 同じイベントの再配信は claim 競合で空 → run は増えない
    again = store.register_ingested(
        tenant,
        source_key=key,
        content_hash=etag,
        document={"id": f"doc_{uuid.uuid4().hex[:24]}", "storage_uri": "s3://x",
                  "original_name": "a.png", "mime_type": "image/png", "page_count": 1,
                  "external_ref": "x"},
        pages=[],
        matches=[match],
    )
    assert again == []
    with owner.begin() as c:
        n = c.execute(
            text("SELECT count(*) FROM workflow_runs WHERE tenant_id=:t"), {"t": tenant}
        ).scalar()
    assert n == 1

    # ETag が変われば別 claim として通る（同一キーの差し替え再処理）
    run_ids2 = store.register_ingested(
        tenant,
        source_key=key,
        content_hash="etag-def",
        document={"id": f"doc_{uuid.uuid4().hex[:24]}", "storage_uri": "s3://x2",
                  "original_name": "a.png", "mime_type": "image/png", "page_count": 1,
                  "external_ref": "x"},
        pages=[],
        matches=[match],
    )
    assert len(run_ids2) == 1


def test_register_ingestedはextractノードの種別をdocuments_doc_typeに書く(seeded) -> None:  # noqa: ANN001
    """自動取込の帳票にも宣言種別を入れる（設計 §11-11）。schema_id 指定は field_schemas の
    doc_type を引く。実 Pg の SQL（UPDATE と SELECT）で成立することを見る。"""
    from sqlalchemy import create_engine, text

    from newfan_orchestrator.workflow_trigger import TriggerMatch

    tenant, wf_active = seeded
    store = _store()
    sch_id = f"sch_{uuid.uuid4().hex[:20]}"
    engine = create_engine(_DSN, future=True)  # type: ignore[arg-type]
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO field_schemas (id, tenant_id, doc_type, version, fields)"
                " VALUES (:i,:t,'invoice',1, CAST(:f AS jsonb))"
            ),
            {"i": sch_id, "t": tenant, "f": json.dumps([{"name": "total_amount", "type": "money_jpy"}])},
        )
    graph = {
        "version": 1,
        "nodes": [
            {"id": "t1", "type": "source.s3_event", "config": {"connection_id": "con_s3x", "prefix": "invoices/"}},
            {"id": "x1", "type": "process.extract", "config": {"schema_id": sch_id}},
        ],
        "edges": [{"from": "t1", "to": "x1"}],
    }
    doc_id = f"doc_{uuid.uuid4().hex[:24]}"
    try:
        run_ids = store.register_ingested(
            tenant,
            source_key="ten_x/invoices/typed.png",
            content_hash="etag-typed",
            document={
                "id": doc_id, "storage_uri": "s3://main/x/original.png", "original_name": "typed.png",
                "mime_type": "image/png", "page_count": 1, "external_ref": "s3://inbox-bkt/ten_x/invoices/typed.png",
            },
            pages=[{"page_no": 1, "width": 10, "height": 20, "image_uri": "s3://main/x/p1.png", "preproc": {}}],
            matches=[TriggerMatch(workflow_id=wf_active, workflow_version=3, graph_json=graph, node_id="t1", connection_id="con_s3x")],
        )
        assert len(run_ids) == 1
        with engine.begin() as c:
            c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant})
            dt = c.execute(text("SELECT doc_type FROM documents WHERE id=:d"), {"d": doc_id}).scalar()
        assert dt == "invoice"
    finally:
        with engine.begin() as c:
            c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant})
            c.execute(text("DELETE FROM field_schemas WHERE id=:i"), {"i": sch_id})
