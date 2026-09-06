from types import SimpleNamespace

from gw_helpers import PDF, auth

from newfan_schemas import ExtractedField
from newfan_gateway.records import RunRecord


def _upload(ctx: SimpleNamespace) -> str:
    r = ctx.client.post(
        "/v1/documents",
        headers=auth("uploader"),
        files={"file": ("invoice.pdf", PDF, "application/pdf")},
    )
    return r.json()["document_id"]


def test_extract_enqueues_and_conflicts(ctx: SimpleNamespace) -> None:
    doc_id = _upload(ctx)
    r = ctx.client.post(f"/v1/documents/{doc_id}/extract", headers=auth("uploader"), json={})
    assert r.status_code == 202
    body = r.json()
    assert body["run_id"].startswith("run_")
    assert body["job_id"].startswith("job_")
    # キューに投入されている
    assert ctx.queue.messages and ctx.queue.messages[0][0] == "q.extract"
    # 実行中 Run と競合 → 409 E1005
    r2 = ctx.client.post(f"/v1/documents/{doc_id}/extract", headers=auth("uploader"), json={})
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "E1005"


def test_extract_idempotency_key(ctx: SimpleNamespace) -> None:
    doc_id = _upload(ctx)
    headers = {**auth("uploader"), "Idempotency-Key": "abc"}
    r1 = ctx.client.post(f"/v1/documents/{doc_id}/extract", headers=headers, json={})
    r2 = ctx.client.post(f"/v1/documents/{doc_id}/extract", headers=headers, json={})
    assert r1.json() == r2.json()  # 同一応答（重複 Run を作らない）


def _seed_needs_review_run(ctx: SimpleNamespace, doc_id: str) -> str:
    run = RunRecord(
        id="run_seed1",
        tenant_id="ten_1",
        document_id=doc_id,
        status="needs_review",
        result_version=1,
        fields=[
            ExtractedField(name="total_amount", value_normalized="128000", confidence=0.72)
        ],
        review_summary={"pending": 2, "auto": 14},
    )
    ctx.repo.create_run(run)
    return run.id


def test_get_result(ctx: SimpleNamespace) -> None:
    doc_id = _upload(ctx)
    _seed_needs_review_run(ctx, doc_id)
    r = ctx.client.get(f"/v1/documents/{doc_id}/result", headers=auth("viewer"))
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "needs_review"
    assert body["result_version"] == 1
    assert body["fields"][0]["name"] == "total_amount"
    assert body["review_summary"]["pending"] == 2


def test_result_exposes_fallback_pages(ctx: SimpleNamespace) -> None:
    doc_id = _upload(ctx)
    run = RunRecord(
        id="run_vl",
        tenant_id="ten_1",
        document_id=doc_id,
        status="needs_review",
        result_version=1,
        fields=[ExtractedField(name="total_amount", value_normalized="1", confidence=0.7)],
        review_summary={"pending": 1, "auto": 0},
        fallback_pages=[2, 3],
    )
    ctx.repo.create_run(run)
    r = ctx.client.get(f"/v1/documents/{doc_id}/result", headers=auth("viewer"))
    assert r.status_code == 200
    assert r.json()["fallback_pages"] == [2, 3]


def test_corrections_optimistic_lock(ctx: SimpleNamespace) -> None:
    doc_id = _upload(ctx)
    run_id = _seed_needs_review_run(ctx, doc_id)
    ok = ctx.client.post(
        f"/v1/documents/{doc_id}/corrections",
        headers=auth("reviewer"),
        json={
            "run_id": run_id,
            "version": 1,
            "items": [
                {"field_name": "total_amount", "original_value": "128000", "corrected_value": "178000"}
            ],
        },
    )
    assert ok.status_code == 200
    assert len(ok.json()["correction_ids"]) == 1

    stale = ctx.client.post(
        f"/v1/documents/{doc_id}/corrections",
        headers=auth("reviewer"),
        json={"run_id": run_id, "version": 99, "items": []},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "E1006"


def test_corrections_requires_reviewer(ctx: SimpleNamespace) -> None:
    doc_id = _upload(ctx)
    run_id = _seed_needs_review_run(ctx, doc_id)
    r = ctx.client.post(
        f"/v1/documents/{doc_id}/corrections",
        headers=auth("uploader"),
        json={"run_id": run_id, "version": 1, "items": []},
    )
    assert r.status_code == 403


def test_confirm_resumes_graph(ctx: SimpleNamespace) -> None:
    doc_id = _upload(ctx)
    run_id = _seed_needs_review_run(ctx, doc_id)
    r = ctx.client.post(
        f"/v1/documents/{doc_id}/confirm", headers=auth("reviewer"), json={"run_id": run_id}
    )
    assert r.status_code == 202
    # confirm は保存済み修正を feedback として渡す（§3.2）。修正が無くても
    # corrections=[] を渡し、apply_feedback に全項目 approved で確定させる。
    assert ctx.orch.resumed == [(run_id, "ten_1", {"corrections": []})]


def test_review_queue(ctx: SimpleNamespace) -> None:
    doc_id = _upload(ctx)
    _seed_needs_review_run(ctx, doc_id)
    r = ctx.client.get("/v1/review/queue", headers=auth("reviewer"))
    assert r.status_code == 200
    items = r.json()["items"]
    assert items and items[0]["pending"] == 2


def test_confirm_relays_workflow_notify(ctx: SimpleNamespace) -> None:
    # ワークフロー起点の Run は options.workflow_notify を持つ（§16 P3）。
    # confirm はそれを再開ジョブへ中継する。中継しないと確定フロー完了の
    # confirm_done が q.workflow に届かず、hitl_gate の run が永久に waiting_hitl のまま
    doc_id = _upload(ctx)
    notify = {"stream": "q.workflow", "workflow_run_id": "wfrun_9"}
    run = RunRecord(
        id="run_wf9",
        tenant_id="ten_1",
        document_id=doc_id,
        status="needs_review",
        result_version=1,
        options={"workflow_idem": "wfrun_9:x1", "workflow_notify": notify},
        fields=[ExtractedField(name="total_amount", value_normalized="1", confidence=0.7)],
        review_summary={"pending": 1, "auto": 0},
    )
    ctx.repo.create_run(run)
    r = ctx.client.post(
        f"/v1/documents/{doc_id}/confirm", headers=auth("reviewer"), json={"run_id": run.id}
    )
    assert r.status_code == 202
    assert ctx.orch.notified[-1] == notify


def test_confirm_without_workflow_passes_no_notify(ctx: SimpleNamespace) -> None:
    # 手動アップロード経路（ワークフロー無関係）の confirm は notify を積まない
    doc_id = _upload(ctx)
    run_id = _seed_needs_review_run(ctx, doc_id)
    ctx.client.post(
        f"/v1/documents/{doc_id}/confirm", headers=auth("reviewer"), json={"run_id": run_id}
    )
    assert ctx.orch.notified[-1] is None


def test_review_queue_reflects_hitl_boost(ctx: SimpleNamespace) -> None:
    # hitl_gate の priority_boost（workflow_runs.waiting 由来）が優先度へ加点される（§16 P5）
    doc_a = _upload(ctx)
    doc_b = _upload(ctx)
    for i, d in enumerate([doc_a, doc_b]):
        ctx.repo.create_run(
            RunRecord(
                id=f"run_q{i}",
                tenant_id="ten_1",
                document_id=d,
                status="needs_review",
                result_version=1,
                fields=[ExtractedField(name="total_amount", value_normalized="1", confidence=0.7)],
                review_summary={"pending": 1, "auto": 0},
            )
        )
    ctx.repo.seed_hitl_boost("ten_1", doc_b, 25)
    r = ctx.client.get("/v1/review/queue", headers=auth("reviewer"))
    items = r.json()["items"]
    assert [i["document_id"] for i in items][0] == doc_b  # boost 側が先頭
    by_doc = {i["document_id"]: i["priority"] for i in items}
    assert by_doc[doc_b] == 26.0 and by_doc[doc_a] == 1.0


def test_result_exposes_schema_id_for_templatize(ctx: SimpleNamespace) -> None:
    """テンプレート化バナー（ADR-0006）の出し分け根拠。

    スキーマレス抽出の run は schema_id=null、スキーマ指定なら id が入る。
    ここが落ちると UI は「どの run が自動発見か」を判定できず、スキーマ指定済みの
    run にまでテンプレート化を出すか、逆に一切出せなくなる。
    """
    # スキーマなし（自動発見）
    doc_a = _upload(ctx)
    ctx.repo.create_run(
        RunRecord(id="run_nosch", tenant_id="ten_1", document_id=doc_a, status="needs_review")
    )
    r = ctx.client.get(f"/v1/documents/{doc_a}/result", headers=auth("viewer"))
    assert r.status_code == 200
    assert r.json()["schema_id"] is None

    # スキーマ指定（conftest の sch_1）
    doc_b = _upload(ctx)
    ctx.repo.create_run(
        RunRecord(
            id="run_sch", tenant_id="ten_1", document_id=doc_b,
            schema_id="sch_1", status="needs_review",
        )
    )
    r = ctx.client.get(f"/v1/documents/{doc_b}/result", headers=auth("viewer"))
    assert r.status_code == 200
    assert r.json()["schema_id"] == "sch_1"


def test_extract_without_schema_is_accepted(ctx: SimpleNamespace) -> None:
    """スキーマ未指定でも 202（テンプレートレス既定, ADR-0006）。

    UI（ExtractStart）はスキーマ選択を任意にした。サーバ側が必須化へ退行すると
    「スキーマ未登録の帳票は永遠に抽出できない」行き止まりが再発する。
    """
    doc_id = _upload(ctx)
    r = ctx.client.post(
        f"/v1/documents/{doc_id}/extract", headers=auth("uploader"), json={"schema_id": None}
    )
    assert r.status_code == 202


# ---------- supersede_review（設計 §3.1 再抽出ボタン / C2・C3・C6） ----------


def test_extract_needs_review_rejected_by_default(ctx: SimpleNamespace) -> None:
    """既定は従来どおり needs_review も競合として弾く（外部連携の二重投入防止）。"""
    doc_id = _upload(ctx)
    _seed_needs_review_run(ctx, doc_id)
    r = ctx.client.post(f"/v1/documents/{doc_id}/extract", headers=auth("uploader"), json={})
    assert r.status_code == 409 and r.json()["error"]["code"] == "E1005"


def test_extract_supersede_review_accepts_needs_review(ctx: SimpleNamespace) -> None:
    """テンプレート化直後の再抽出は「自動発見 run が needs_review」が典型状態。

    既定のままではこのボタンが必ず 409 になるので、明示フラグのときだけ通す。
    旧 run は superseded へ落とす——残すと get_latest_run・削除ブロッカー・
    ワークフローの hitl_gate が古い run を見続ける。
    """
    doc_id = _upload(ctx)
    old_run = _seed_needs_review_run(ctx, doc_id)
    r = ctx.client.post(
        f"/v1/documents/{doc_id}/extract",
        headers=auth("uploader"),
        json={"supersede_review": True},
    )
    assert r.status_code == 202
    assert ctx.repo.get_run("ten_1", old_run).status == "superseded"
    # 検証画面は新しい run を見る
    latest = ctx.repo.get_latest_run("ten_1", doc_id)
    assert latest is not None and latest.id == r.json()["run_id"]


def test_extract_supersede_review_still_rejects_processing(ctx: SimpleNamespace) -> None:
    """処理中の run は supersede_review でも弾く（worker が走っている）。"""
    doc_id = _upload(ctx)
    ctx.client.post(f"/v1/documents/{doc_id}/extract", headers=auth("uploader"), json={})
    r = ctx.client.post(
        f"/v1/documents/{doc_id}/extract",
        headers=auth("uploader"),
        json={"supersede_review": True},
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "E1005"


def test_extract_rejects_confirmed_document(ctx: SimpleNamespace) -> None:
    """確定済みは supersede_review でも拒否する。

    会計連携済みの確定値を無警告で置き換えないため。UI 側でもボタンを出さないが、
    API 直叩きでも守る。
    """
    doc_id = _upload(ctx)
    run = _seed_needs_review_run(ctx, doc_id)
    ctx.repo.get_run("ten_1", run).status = "confirmed"
    r = ctx.client.post(
        f"/v1/documents/{doc_id}/extract",
        headers=auth("uploader"),
        json={"supersede_review": True},
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "E1005"
    assert ctx.repo.get_run("ten_1", run).status == "confirmed"  # 触っていない
