"""DELETE /v1/documents/{id}（§6.2 取り込んだ帳票の削除）。

削除は不可逆で、しかも FK CASCADE が効かない表（correction_logs / jobs /
workflow_runs）が DDL 上いくつもある。ここで固定したいのは 2 つの壊れ方:

1. 「消えたつもりで残る」— DB 行だけ消えて実体・学習ログ・キュー内ジョブが残る
2. 「実行中に消してワーカーを毒メッセージ地獄に落とす」— 参照先を失ったジョブが
   試行上限なしで無限再配信される

InMemory 実装での検証なので、実 DDL 側の契約（孤児の不在・監査行・RLS）は
test_pg_repository_integration.py が別途 pin する。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from gw_helpers import PDF, auth, make_token

from newfan_gateway.records import CorrectionRecord, RunRecord, WorkflowRunRecord


def _upload(ctx: SimpleNamespace) -> str:
    r = ctx.client.post(
        "/v1/documents",
        headers=auth("uploader"),
        files={"file": ("invoice.pdf", PDF, "application/pdf")},
        data={"doc_type": "invoice", "external_ref": "ERP-1"},
    )
    assert r.status_code == 201, r.text
    return str(r.json()["document_id"])


def _seed_run(ctx: SimpleNamespace, doc_id: str, *, status: str = "needs_review") -> str:
    run = RunRecord(id=f"run_{doc_id}", tenant_id="ten_1", document_id=doc_id, status=status)
    ctx.repo.create_run(run)
    return run.id


def _delete(ctx: SimpleNamespace, doc_id: str, role: str = "reviewer") -> Any:
    return ctx.client.delete(f"/v1/documents/{doc_id}", headers=auth(role))


# --- 正常系 ---


def test_delete_returns_counts(ctx: SimpleNamespace) -> None:
    """件数を返すのは、学習例まで消えることを UI が黙らずに伝えられるようにするため。"""
    doc_id = _upload(ctx)
    run_id = _seed_run(ctx, doc_id)
    ctx.repo.add_corrections(
        [
            CorrectionRecord(
                id="cor_1",
                tenant_id="ten_1",
                document_id=doc_id,
                run_id=run_id,
                field_name="total_amount",
                corrected_value="1",
            )
        ]
    )
    r = _delete(ctx, doc_id)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deleted"] is True
    assert body["runs_deleted"] == 1
    assert body["corrections_deleted"] == 1
    assert body["objects_deleted"] >= 1  # 原本＋ページ画像の実ファイル


def test_delete_removes_local_files(ctx: SimpleNamespace, tmp_path: Any) -> None:
    """DB 行だけ消して実体を残すと、消したはずの帳票が保管先に残り続ける。"""
    doc_id = _upload(ctx)
    doc_dir = tmp_path / "ten_1" / doc_id
    assert doc_dir.is_dir()
    assert _delete(ctx, doc_id).status_code == 200
    assert not doc_dir.exists()


def test_delete_then_all_reads_are_e1001(ctx: SimpleNamespace) -> None:
    doc_id = _upload(ctx)
    assert _delete(ctx, doc_id).status_code == 200
    for path in (f"/v1/documents/{doc_id}", f"/v1/documents/{doc_id}/result"):
        r = ctx.client.get(path, headers=auth("viewer"))
        assert r.status_code == 400, path
        assert r.json()["error"]["code"] == "E1001", path
    r = ctx.client.post(f"/v1/documents/{doc_id}/extract", headers=auth("uploader"), json={})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "E1001"


def test_delete_disappears_from_list(ctx: SimpleNamespace) -> None:
    keep = _upload(ctx)
    gone = _upload(ctx)
    assert _delete(ctx, gone).status_code == 200
    items = ctx.client.get("/v1/documents", headers=auth("viewer")).json()["items"]
    assert [d["document_id"] for d in items] == [keep]


def test_delete_releases_own_lock(ctx: SimpleNamespace) -> None:
    """ロックを残すと、同じ id が再利用されない以上ただのゴミだが、
    保持者本人の他画面が readOnly のまま固まる。"""
    doc_id = _upload(ctx)
    assert ctx.client.post(f"/v1/documents/{doc_id}/lock", headers=auth("reviewer")).status_code == 200
    assert _delete(ctx, doc_id).status_code == 200
    assert ctx.client.app.state.lock_store.get("ten_1", doc_id) is None


# --- 冪等性・不在・テナント分離 ---


def test_delete_twice_second_is_e1001(ctx: SimpleNamespace) -> None:
    doc_id = _upload(ctx)
    assert _delete(ctx, doc_id).status_code == 200
    again = _delete(ctx, doc_id)
    assert again.status_code == 400
    assert again.json()["error"]["code"] == "E1001"


def test_delete_nonexistent_is_400_not_404(ctx: SimpleNamespace) -> None:
    """§6.5 の contract は E1001→400。404 に変えると既存クライアントが壊れる。"""
    r = _delete(ctx, "doc_nope")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "E1001"


def test_delete_other_tenant_is_rejected_and_original_survives(ctx: SimpleNamespace) -> None:
    doc_id = _upload(ctx)  # ten_1
    other = {"Authorization": f"Bearer {make_token(role='reviewer', tenant='ten_2')}"}
    r = ctx.client.delete(f"/v1/documents/{doc_id}", headers=other)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "E1001"
    assert ctx.client.get(f"/v1/documents/{doc_id}", headers=auth("viewer")).status_code == 200


# --- 権限 ---


def test_delete_requires_reviewer(ctx: SimpleNamespace) -> None:
    doc_id = _upload(ctx)
    for role in ("viewer", "uploader"):
        r = _delete(ctx, doc_id, role)
        assert r.status_code == 403, role
        assert r.json()["error"]["code"] == "E5001", role
    assert ctx.client.get(f"/v1/documents/{doc_id}", headers=auth("viewer")).status_code == 200


def test_delete_rejects_m2m_api_key(ctx: SimpleNamespace) -> None:
    """api ロール（rank 2）の外部連携から大量に消せると事故が青天井になる。"""
    doc_id = _upload(ctx)
    r = ctx.client.delete(f"/v1/documents/{doc_id}", headers={"X-Api-Key": "api-key-123"})
    assert r.status_code == 403


# --- 拒否条件 ---


def test_delete_rejects_processing_run(ctx: SimpleNamespace) -> None:
    """実行中に消すとワーカーが参照先を失って無限再配信になる。"""
    doc_id = _upload(ctx)
    _seed_run(ctx, doc_id, status="processing")
    r = _delete(ctx, doc_id)
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "E1005"
    assert err["details"]["reason"] == "processing"


def test_delete_allows_needs_review_run(ctx: SimpleNamespace) -> None:
    """レビュー待ちのまま不要と判明した帳票こそ削除需要の中心。塞いではいけない。"""
    doc_id = _upload(ctx)
    _seed_run(ctx, doc_id, status="needs_review")
    assert _delete(ctx, doc_id).status_code == 200


def test_delete_rejects_document_in_review(ctx: SimpleNamespace) -> None:
    """confirm は documents だけ in_review にして run は needs_review のまま残す。

    run.status しか見ないと確定処理の最中に削除が通り、直後に resume した
    ワーカーが孤児の学習ログを作って webhook を外部へ飛ばす。
    """
    doc_id = _upload(ctx)
    _seed_run(ctx, doc_id, status="needs_review")
    ctx.repo.set_document_status("ten_1", doc_id, "in_review")
    r = _delete(ctx, doc_id)
    assert r.status_code == 409
    assert r.json()["error"]["details"]["reason"] == "document_busy"


def test_delete_allows_stale_processing_run(ctx: SimpleNamespace) -> None:
    """mark_run_failed はワーカーの例外ハンドラ経由でしか呼ばれない。

    Fargate のタスク入れ替えや OOM では processing が永久固着するため、
    時間で見切らないと「消したい帳票ほど消せない」になる。
    """
    doc_id = _upload(ctx)
    run_id = _seed_run(ctx, doc_id, status="processing")
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    ctx.repo._runs[run_id].started_at = old  # noqa: SLF001
    ctx.repo._doc_touched[doc_id] = old  # noqa: SLF001
    assert _delete(ctx, doc_id).status_code == 200


def test_delete_rejects_running_workflow(ctx: SimpleNamespace) -> None:
    doc_id = _upload(ctx)
    ctx.client.app.state.workflows.create_run(
        WorkflowRunRecord(
            id="wfr_1",
            tenant_id="ten_1",
            workflow_id="wf_1",
            workflow_version=1,
            trigger={"type": "manual"},
            document_id=doc_id,
            status="running",
        )
    )
    r = _delete(ctx, doc_id)
    assert r.status_code == 409
    assert r.json()["error"]["details"]["reason"] == "workflow_active"


def test_delete_allows_waiting_hitl_workflow(ctx: SimpleNamespace) -> None:
    """waiting_hitl を止める API は無い。拒否すると永久に削除できなくなる。"""
    doc_id = _upload(ctx)
    ctx.client.app.state.workflows.create_run(
        WorkflowRunRecord(
            id="wfr_1",
            tenant_id="ten_1",
            workflow_id="wf_1",
            workflow_version=1,
            trigger={"type": "manual"},
            document_id=doc_id,
            status="waiting_hitl",
        )
    )
    assert _delete(ctx, doc_id).status_code == 200


def test_delete_rejects_lock_held_by_other(ctx: SimpleNamespace) -> None:
    doc_id = _upload(ctx)
    alice = {"Authorization": f"Bearer {make_token(role='reviewer', sub='alice')}"}
    assert ctx.client.post(f"/v1/documents/{doc_id}/lock", headers=alice).status_code == 200
    r = _delete(ctx, doc_id)  # sub=u1
    assert r.status_code == 409
    details = r.json()["error"]["details"]
    assert details["reason"] == "locked"
    assert details["holder"] == "alice"


def test_delete_allows_own_lock(ctx: SimpleNamespace) -> None:
    doc_id = _upload(ctx)
    assert ctx.client.post(f"/v1/documents/{doc_id}/lock", headers=auth("reviewer")).status_code == 200
    assert _delete(ctx, doc_id).status_code == 200


# --- 実体の削除が失敗したとき ---


def test_delete_keeps_db_row_when_storage_fails(ctx: SimpleNamespace) -> None:
    """S3→DB の順序。逆だと storage_uri を失って孤児オブジェクトを辿れなくなる。

    実体が消せていない以上 DB は無傷で残し、一覧から再試行できる状態で止める。
    """

    def _boom(prefix: str) -> int:
        raise RuntimeError("S3 down")

    doc_id = _upload(ctx)
    ctx.client.app.state.object_store.delete_prefix = _boom
    r = _delete(ctx, doc_id)
    assert r.status_code == 500
    assert r.json()["error"]["code"] == "E2000"
    assert ctx.client.get(f"/v1/documents/{doc_id}", headers=auth("viewer")).status_code == 200


def test_delete_prefix_is_scoped_to_tenant_and_document(ctx: SimpleNamespace) -> None:
    """prefix を間違えるとテナント丸ごと消える。渡している値そのものを固定する。"""
    seen: list[str] = []
    doc_id = _upload(ctx)
    ctx.client.app.state.object_store.delete_prefix = lambda p: (seen.append(p), 0)[1]
    assert _delete(ctx, doc_id).status_code == 200
    assert seen == [f"ten_1/{doc_id}/"]


# --- 監査 ---


def test_delete_writes_audit_row(ctx: SimpleNamespace) -> None:
    """audit_logs はアプリロールから消せない。削除の唯一の恒久証跡になる。"""
    doc_id = _upload(ctx)
    assert _delete(ctx, doc_id).status_code == 200
    assert len(ctx.repo.audits) == 1
    a = ctx.repo.audits[0]
    assert a["action"] == "document.delete"
    assert a["target_type"] == "document"
    assert a["target_id"] == doc_id
    assert a["actor_id"] == "u1"


def test_rejected_delete_writes_no_audit(ctx: SimpleNamespace) -> None:
    doc_id = _upload(ctx)
    _seed_run(ctx, doc_id, status="processing")
    assert _delete(ctx, doc_id).status_code == 409
    assert ctx.repo.audits == []


def test_concurrent_delete_loser_gets_e1001_not_500(ctx: SimpleNamespace) -> None:
    """同時に 2 回 DELETE した敗者。

    delete_document は「消す直前に他から消されていた」を DocumentGoneError で
    知らせる。router がこれを翻訳しないと 500「内部エラー」になり、利用者には
    何が起きたか伝わらない（実体はもう無いので「見つかりません」が正しい）。
    """
    from newfan_gateway.repository import DocumentGoneError

    doc_id = _upload(ctx)

    def _lost(*a: Any, **k: Any) -> None:
        raise DocumentGoneError(doc_id)

    ctx.repo.delete_document = _lost
    r = _delete(ctx, doc_id)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "E1001"
