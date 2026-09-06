"""PgRepository.delete_document を実 PostgreSQL に対して検証する（§6.2 / §7）。

InMemory テストでは絶対に検出できないことだけをここで pin する:

- **孤児**: correction_logs / jobs / workflow_runs は documents を TEXT 列で指すだけで、
  0005 の FK を張るまで DB は何もしてくれなかった。「アプリが明示的に消す」ことが
  契約であって、DDL 任せにはできない。
- **実 DDL との整合**: 存在しない列を書くと InMemory は通るのに本番だけ
  UndefinedColumn → 500 になる（correction_logs の note 事故と同型。実 AWS で再発）。
- **監査**: record_audit（workflows 用）は target_type='workflow' 直書きなので、
  そのまま流用すると document の削除が workflow として記録される。

DATABASE_URL_TEST が設定されている時だけ動く。
例: postgresql+psycopg://newfan:newfan@localhost:5433/newfan
"""

from __future__ import annotations

import os
import uuid

import pytest

pytest.importorskip("sqlalchemy", reason="PgRepository は runtime 依存")
pytest.importorskip("psycopg", reason="PgRepository は runtime 依存")

from sqlalchemy import text  # noqa: E402

_DSN = os.environ.get("DATABASE_URL_TEST")
pytestmark = pytest.mark.skipif(not _DSN, reason="DATABASE_URL_TEST 未設定（実 DB が要る）")

TENANT = "ten_del_test"


@pytest.fixture
def repo():
    from newfan_gateway.db import PgRepository

    return PgRepository(_DSN)  # type: ignore[arg-type]


def _sql(repo, stmt: str, **params):
    with repo._engine.begin() as c:  # noqa: SLF001
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT})
        return c.execute(text(stmt), params).all()


def _count(repo, stmt: str, **params) -> int:
    return int(_sql(repo, stmt, **params)[0][0])


@pytest.fixture
def doc(repo):
    """documents / pages / extraction_runs / fields / tables / correction_logs /
    jobs / workflow_runs / workflow_node_runs を一式そろえた帳票を作る。"""
    from newfan_gateway.records import DocumentRecord, PageRecord, RunRecord

    sfx = uuid.uuid4().hex[:12]
    doc_id, run_id = f"doc_{sfx}", f"run_{sfx}"
    wf_id, wfr_id = f"wf_{sfx}", f"wfr_{sfx}"

    with repo._engine.begin() as c:  # noqa: SLF001
        c.execute(
            text("INSERT INTO tenants (id, name) VALUES (:i,:n) ON CONFLICT (id) DO NOTHING"),
            {"i": TENANT, "n": "delete-test"},
        )
    repo.create_document(
        DocumentRecord(
            id=doc_id, tenant_id=TENANT, storage_uri="s3://b/k", mime_type="image/png",
            page_count=1, doc_type="invoice", status="uploaded",
        ),
        [PageRecord(page_no=1, width=10, height=10, image_uri="s3://b/p1.png")],
    )
    repo.create_run(RunRecord(id=run_id, tenant_id=TENANT, document_id=doc_id))

    with repo._engine.begin() as c:  # noqa: SLF001
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT})
        c.execute(
            text(
                "INSERT INTO extraction_fields (id, tenant_id, run_id, field_name, value_raw)"
                " VALUES (:i,:t,:r,'total_amount','128000')"
            ),
            {"i": f"fld_{sfx}", "t": TENANT, "r": run_id},
        )
        c.execute(
            text(
                "INSERT INTO extraction_tables (id, tenant_id, run_id, name, rows)"
                " VALUES (:i,:t,:r,'items','[]'::jsonb)"
            ),
            {"i": f"tbl_{sfx}", "t": TENANT, "r": run_id},
        )
        c.execute(
            text(
                "INSERT INTO correction_logs (id, tenant_id, document_id, run_id,"
                " field_name, corrected_value) VALUES (:i,:t,:d,:r,'total_amount','999')"
            ),
            {"i": f"cor_{sfx}", "t": TENANT, "d": doc_id, "r": run_id},
        )
        # tenant_memories は correction_logs の FK CASCADE で連鎖するはず
        c.execute(
            text(
                "INSERT INTO tenant_memories (id, tenant_id, correction_log_id,"
                " faiss_vector_id, embed_model) VALUES (:i,:t,:c,:v,'test')"
            ),
            {"i": f"mem_{sfx}", "t": TENANT, "c": f"cor_{sfx}", "v": abs(hash(sfx)) % 10**9},
        )
        c.execute(
            text(
                "INSERT INTO jobs (id, tenant_id, kind, ref_id) VALUES (:i,:t,'extract',:r)"
            ),
            {"i": f"job_{sfx}", "t": TENANT, "r": run_id},
        )
        c.execute(
            text(
                "INSERT INTO workflows (id, tenant_id, name, graph_json)"
                " VALUES (:i,:t,'wf','{}'::jsonb)"
            ),
            {"i": wf_id, "t": TENANT},
        )
        c.execute(
            text(
                "INSERT INTO workflow_runs (id, tenant_id, workflow_id, workflow_version,"
                " trigger, document_id, status) VALUES (:i,:t,:w,1,'{}'::jsonb,:d,'waiting_hitl')"
            ),
            {"i": wfr_id, "t": TENANT, "w": wf_id, "d": doc_id},
        )
        c.execute(
            text(
                "INSERT INTO workflow_node_runs (id, tenant_id, workflow_run_id, node_id,"
                " node_type, input, output) VALUES (:i,:t,:r,'n1','extract',"
                " CAST(:inp AS jsonb), CAST(:out AS jsonb))"
            ),
            {
                "i": f"nr_{sfx}",
                "t": TENANT,
                "r": wfr_id,
                # 抽出値が生で入る想定。削除で消えることを確かめたい
                "inp": '{"total_amount": "128000"}',
                "out": '{"total_amount": "128000"}',
            },
        )
    yield {"doc_id": doc_id, "run_id": run_id, "wf_id": wf_id, "wfr_id": wfr_id}

    # 後始末（テストが途中で落ちても残さない）
    with repo._engine.begin() as c:  # noqa: SLF001
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT})
        for stmt in (
            "DELETE FROM workflow_node_runs WHERE tenant_id=:t",
            "DELETE FROM workflow_runs WHERE tenant_id=:t",
            "DELETE FROM workflows WHERE tenant_id=:t",
            "DELETE FROM jobs WHERE tenant_id=:t",
            "DELETE FROM correction_logs WHERE tenant_id=:t",
            "DELETE FROM extraction_runs WHERE tenant_id=:t",
            "DELETE FROM documents WHERE tenant_id=:t",
            "DELETE FROM audit_logs WHERE tenant_id=:t",
        ):
            c.execute(text(stmt), {"t": TENANT})


def test_delete_document_matches_real_ddl(repo, doc) -> None:
    """実 DDL に無い列を触っていないこと。ここが通れば本番で 500 にならない。"""
    counts = repo.delete_document(
        TENANT, doc["doc_id"], actor_id="sato", detail={"objects_deleted": 3}
    )
    assert counts is not None
    assert counts["runs_deleted"] == 1
    assert counts["corrections_deleted"] == 1
    assert counts["jobs_deleted"] == 1
    assert repo.get_document(TENANT, doc["doc_id"]) is None


def test_delete_cascades_pages_runs_fields_tables(repo, doc) -> None:
    """FK CASCADE に任せている 4 表。DDL が変わって CASCADE が外れたら気付けるように。"""
    repo.delete_document(TENANT, doc["doc_id"], actor_id="sato", detail={})
    d, r = doc["doc_id"], doc["run_id"]
    assert _count(repo, "SELECT count(*) FROM pages WHERE document_id=:d", d=d) == 0
    assert _count(repo, "SELECT count(*) FROM extraction_runs WHERE document_id=:d", d=d) == 0
    assert _count(repo, "SELECT count(*) FROM extraction_fields WHERE run_id=:r", r=r) == 0
    assert _count(repo, "SELECT count(*) FROM extraction_tables WHERE run_id=:r", r=r) == 0


def test_delete_leaves_no_orphan_corrections_or_jobs(repo, doc) -> None:
    """FK CASCADE が効かない表。DB は守ってくれないのでアプリの契約として固定する。

    ここが緩むと、消したはずの原本の値（original_value / corrected_value）が
    correction_logs に残り、jobs は参照先を失って無限に再配信される。
    """
    repo.delete_document(TENANT, doc["doc_id"], actor_id="sato", detail={})
    d, r = doc["doc_id"], doc["run_id"]
    assert _count(repo, "SELECT count(*) FROM correction_logs WHERE document_id=:d", d=d) == 0
    assert _count(repo, "SELECT count(*) FROM jobs WHERE ref_id=:r", r=r) == 0


def test_delete_cascades_tenant_memories(repo, doc) -> None:
    """correction_logs を消せば tenant_memories も連鎖する（0001 の FK）。

    残すと FAISS のベクトル id だけが宙に浮き、検索が消えた帳票の値を返す。
    """
    repo.delete_document(TENANT, doc["doc_id"], actor_id="sato", detail={})
    assert _count(repo, "SELECT count(*) FROM tenant_memories WHERE tenant_id=:t", t=TENANT) == 0


def test_delete_keeps_workflow_run_but_detaches_and_terminates(repo, doc) -> None:
    """ワークフロー実行履歴は運用の証跡なので行は残す。ただし:

    - document_id は切る（消えた帳票を指し続けない）
    - node_runs の input/output には抽出値が生で入るので NULL 化する
    - waiting_hitl は再開先が消えた以上続行不能。failed に終端化しないと
      list_hitl_boosts に残り続けてレビューキューの優先度を汚す
    """
    repo.delete_document(TENANT, doc["doc_id"], actor_id="sato", detail={})
    rows = _sql(
        repo,
        "SELECT document_id, status, state->>'document_deleted', error->>'code'"
        " FROM workflow_runs WHERE id=:i",
        i=doc["wfr_id"],
    )
    assert len(rows) == 1
    document_id, status, deleted_flag, err = rows[0]
    assert document_id is None
    assert status == "failed"
    assert deleted_flag == "true"
    assert err == "E1001"

    io_rows = _sql(
        repo,
        "SELECT input, output FROM workflow_node_runs WHERE workflow_run_id=:i",
        i=doc["wfr_id"],
    )
    assert io_rows == [(None, None)]


def test_delete_writes_audit_with_document_target_type(repo, doc) -> None:
    """audit_logs はアプリロールから消せない（ensure_app_role）。唯一の恒久証跡。

    PgWorkflowsRepository.record_audit は target_type='workflow' 直書きなので、
    流用すると帳票の削除が workflow として記録されてしまう。
    """
    repo.delete_document(
        TENANT, doc["doc_id"], actor_id="sato", detail={"objects_deleted": 3}
    )
    rows = _sql(
        repo,
        "SELECT actor_type, actor_id, action, target_type, target_id, detail"
        " FROM audit_logs WHERE tenant_id=:t AND target_id=:d",
        t=TENANT,
        d=doc["doc_id"],
    )
    assert len(rows) == 1
    actor_type, actor_id, action, target_type, target_id, detail = rows[0]
    assert (actor_type, actor_id, action) == ("human", "sato", "document.delete")
    assert target_type == "document"
    assert target_id == doc["doc_id"]
    # 復旧・照会の手掛かり。storage_uri と run_ids が無いと孤児オブジェクトを辿れない
    assert detail["storage_uri"] == "s3://b/k"
    assert detail["run_ids"] == [doc["run_id"]]
    assert detail["objects_deleted"] == 3
    assert detail["doc_type"] == "invoice"


def test_delete_other_tenant_returns_none_and_keeps_row(repo, doc) -> None:
    assert repo.delete_document("ten_other", doc["doc_id"], actor_id="x", detail={}) is None
    assert repo.get_document(TENANT, doc["doc_id"]) is not None


def test_delete_missing_returns_none(repo) -> None:
    assert repo.delete_document(TENANT, "doc_nope", actor_id="x", detail={}) is None


def test_delete_is_idempotent(repo, doc) -> None:
    assert repo.delete_document(TENANT, doc["doc_id"], actor_id="sato", detail={}) is not None
    assert repo.delete_document(TENANT, doc["doc_id"], actor_id="sato", detail={}) is None
    # 2 回目は監査行を増やさない
    assert (
        _count(
            repo,
            "SELECT count(*) FROM audit_logs WHERE tenant_id=:t AND target_id=:d",
            t=TENANT,
            d=doc["doc_id"],
        )
        == 1
    )


def test_add_corrections_skips_deleted_document(repo, doc) -> None:
    """オートセーブ（500ms デバウンス）と削除は容易に競合する。

    0005 の FK があるので素の INSERT なら ForeignKeyViolation → 500 になる。
    静かに捨てるのが正しい（消した帳票の値を復活させても誰の役にも立たない）。
    """
    from newfan_gateway.records import CorrectionRecord

    doc_id, run_id = doc["doc_id"], doc["run_id"]
    repo.delete_document(TENANT, doc_id, actor_id="sato", detail={})
    repo.add_corrections(
        [
            CorrectionRecord(
                id=f"cor_after_{uuid.uuid4().hex[:8]}",
                tenant_id=TENANT,
                document_id=doc_id,
                run_id=run_id,
                field_name="total_amount",
                corrected_value="1",
            )
        ]
    )  # 例外にならないこと
    assert _count(repo, "SELECT count(*) FROM correction_logs WHERE document_id=:d", d=doc_id) == 0


# --- 拒否判定（get_delete_blocker） ---


def test_blocker_none_for_needs_review_document(repo, doc) -> None:
    """レビュー待ちのまま不要と判明した帳票こそ削除需要の中心。塞いではいけない。

    fixture の create_run は status 既定が processing なので、抽出完了後の状態に
    そろえてから確かめる（＝抽出中は消せないことは別テストで固定する）。
    """
    with repo._engine.begin() as c:  # noqa: SLF001
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT})
        c.execute(
            text("UPDATE extraction_runs SET status='needs_review' WHERE id=:r"),
            {"r": doc["run_id"]},
        )
    assert repo.get_delete_blocker(TENANT, doc["doc_id"], stale_minutes=30) is None


def test_blocker_detects_in_review_document(repo, doc) -> None:
    """confirm 直後の窓。documents だけ in_review で run は needs_review のまま。"""
    repo.set_document_status(TENANT, doc["doc_id"], "in_review")
    assert repo.get_delete_blocker(TENANT, doc["doc_id"], stale_minutes=30) == "document_busy"


def test_blocker_treats_stale_processing_as_deletable(repo, doc) -> None:
    """タスク入れ替え・OOM で固着した processing は時間で見切る。

    これが無いと「消したい帳票ほど消せない」になる。
    """
    with repo._engine.begin() as c:  # noqa: SLF001
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT})
        c.execute(
            text(
                "UPDATE documents SET status='processing',"
                " updated_at = now() - interval '2 hours' WHERE id=:d"
            ),
            {"d": doc["doc_id"]},
        )
        c.execute(
            text(
                "UPDATE extraction_runs SET status='processing',"
                " started_at = now() - interval '2 hours' WHERE id=:r"
            ),
            {"r": doc["run_id"]},
        )
    assert repo.get_delete_blocker(TENANT, doc["doc_id"], stale_minutes=30) is None


def test_blocker_detects_fresh_processing_run(repo, doc) -> None:
    with repo._engine.begin() as c:  # noqa: SLF001
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT})
        c.execute(
            text("UPDATE extraction_runs SET status='processing' WHERE id=:r"),
            {"r": doc["run_id"]},
        )
    assert repo.get_delete_blocker(TENANT, doc["doc_id"], stale_minutes=30) == "processing"


def test_set_document_status_advances_updated_at(repo, doc) -> None:
    """updated_at を進めないと document_busy 判定が gateway 由来の遷移に対して
    永久に発火せず、確定処理中の窓が無言で素通りする。"""
    before = _sql(repo, "SELECT updated_at FROM documents WHERE id=:d", d=doc["doc_id"])[0][0]
    repo.set_document_status(TENANT, doc["doc_id"], "in_review")
    after = _sql(repo, "SELECT updated_at FROM documents WHERE id=:d", d=doc["doc_id"])[0][0]
    assert after > before
