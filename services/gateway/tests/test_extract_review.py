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
    assert ctx.orch.resumed == [(run_id, None)]


def test_review_queue(ctx: SimpleNamespace) -> None:
    doc_id = _upload(ctx)
    _seed_needs_review_run(ctx, doc_id)
    r = ctx.client.get("/v1/review/queue", headers=auth("reviewer"))
    assert r.status_code == 200
    items = r.json()["items"]
    assert items and items[0]["pending"] == 2
