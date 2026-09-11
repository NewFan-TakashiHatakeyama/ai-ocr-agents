"""GET /documents/{id}/spans（設計 docs/design/region-field-add-and-hint-v2.md §2.4 / D12）。

テンプレート化画面が「枠に含まれる文字」を出すための取得 API。固定する契約:

- 未抽出（run が無い）は E1001。行が無いページは **空配列で 200**（0008 より前の
  run や範囲外ページをエラーにすると、画面が枠を引くたびに赤くなる）。
- page で絞れる（他ページの span が混ざらない）。
- viewer で読める（テンプレート化はレビュー中に開かれる）。
- 他テナントの帳票は E1001（帳票そのものが見えない）。
- 応答は span_id / text / bbox だけ（conf 等の内部値は返さない）。
"""

from __future__ import annotations

from types import SimpleNamespace

from gw_helpers import PDF, auth, make_token

from newfan_gateway.records import RunRecord


def _upload(ctx: SimpleNamespace) -> str:
    r = ctx.client.post(
        "/v1/documents",
        headers=auth("uploader"),
        files={"file": ("invoice.pdf", PDF, "application/pdf")},
    )
    return r.json()["document_id"]


def _seed_run_with_spans(ctx: SimpleNamespace, doc_id: str, run_id: str = "run_spans1") -> str:
    ctx.repo.create_run(
        RunRecord(id=run_id, tenant_id="ten_1", document_id=doc_id, status="needs_review")
    )
    ctx.repo.seed_run_spans(
        run_id, 1,
        [
            {"span_id": 1, "text": "株式会社千曲川ホーム", "bbox": [10, 20, 110, 40], "conf": 0.95},
            {"span_id": 2, "text": "御請求書", "bbox": None, "conf": 0.8},
        ],
    )
    ctx.repo.seed_run_spans(
        run_id, 2, [{"span_id": 3, "text": "2 ページ目", "bbox": [5, 5, 50, 15], "conf": 0.9}]
    )
    return run_id


def test_未抽出はE1001(ctx: SimpleNamespace) -> None:
    doc_id = _upload(ctx)
    r = ctx.client.get(f"/v1/documents/{doc_id}/spans?page=1", headers=auth("viewer"))
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "E1001"


def test_viewerで最新runのspanをページ指定で読める(ctx: SimpleNamespace) -> None:
    doc_id = _upload(ctx)
    run_id = _seed_run_with_spans(ctx, doc_id)
    r = ctx.client.get(f"/v1/documents/{doc_id}/spans?page=1", headers=auth("viewer"))
    assert r.status_code == 200
    body = r.json()
    assert body["run_id"] == run_id
    assert body["page_no"] == 1
    # conf は返さない（応答は span_id / text / bbox だけ）
    assert body["spans"] == [
        {"span_id": 1, "text": "株式会社千曲川ホーム", "bbox": [10, 20, 110, 40]},
        {"span_id": 2, "text": "御請求書", "bbox": None},
    ]


def test_pageで絞れる(ctx: SimpleNamespace) -> None:
    doc_id = _upload(ctx)
    _seed_run_with_spans(ctx, doc_id)
    r = ctx.client.get(f"/v1/documents/{doc_id}/spans?page=2", headers=auth("viewer"))
    assert r.status_code == 200
    assert [s["span_id"] for s in r.json()["spans"]] == [3]
    # page 省略は 1 ページ目
    r = ctx.client.get(f"/v1/documents/{doc_id}/spans", headers=auth("viewer"))
    assert r.status_code == 200
    assert r.json()["page_no"] == 1
    assert [s["span_id"] for s in r.json()["spans"]] == [1, 2]


def test_行が無いページは空配列で200(ctx: SimpleNamespace) -> None:
    """0008 より前の run・失敗 run・範囲外ページをエラーにしない。"""
    doc_id = _upload(ctx)
    _seed_run_with_spans(ctx, doc_id)
    r = ctx.client.get(f"/v1/documents/{doc_id}/spans?page=9", headers=auth("viewer"))
    assert r.status_code == 200
    assert r.json()["spans"] == []
    # span を一切持たない run でも同じ
    doc2 = _upload(ctx)
    ctx.repo.create_run(RunRecord(id="run_nospans", tenant_id="ten_1", document_id=doc2))
    r = ctx.client.get(f"/v1/documents/{doc2}/spans?page=1", headers=auth("viewer"))
    assert r.status_code == 200
    assert r.json() == {"run_id": "run_nospans", "page_no": 1, "spans": []}


def test_他テナントの帳票はE1001(ctx: SimpleNamespace) -> None:
    doc_id = _upload(ctx)
    _seed_run_with_spans(ctx, doc_id)
    other = {"Authorization": f"Bearer {make_token(role='viewer', tenant='ten_2')}"}
    r = ctx.client.get(f"/v1/documents/{doc_id}/spans?page=1", headers=other)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "E1001"


def test_最新runのspanを返す(ctx: SimpleNamespace) -> None:
    """再抽出で run が 2 本になったら、新しい方の span を返す（古い枠の文字を出さない）。"""
    from datetime import datetime, timedelta, timezone

    doc_id = _upload(ctx)
    _seed_run_with_spans(ctx, doc_id, run_id="run_old")
    old = ctx.repo.get_run("ten_1", "run_old")
    assert old is not None
    old.started_at = datetime.now(timezone.utc) - timedelta(hours=1)
    ctx.repo.create_run(
        RunRecord(id="run_new", tenant_id="ten_1", document_id=doc_id, status="needs_review")
    )
    ctx.repo.seed_run_spans(
        "run_new", 1, [{"span_id": 1, "text": "新しい run", "bbox": [1, 2, 3, 4], "conf": 0.9}]
    )
    r = ctx.client.get(f"/v1/documents/{doc_id}/spans?page=1", headers=auth("viewer"))
    assert r.status_code == 200
    assert r.json()["run_id"] == "run_new"
    assert [s["text"] for s in r.json()["spans"]] == ["新しい run"]


def test_帳票削除でInMemoryのrun_spansも消える(ctx: SimpleNamespace) -> None:
    """Pg の FK CASCADE と同じ観測結果を InMemory でも返す（テストが本番と食い違わない）。"""
    doc_id = _upload(ctx)
    run_id = _seed_run_with_spans(ctx, doc_id)
    ctx.repo.set_document_status("ten_1", doc_id, "confirmed")
    assert ctx.repo.delete_document("ten_1", doc_id, actor_id="u1", detail={}) is not None
    assert ctx.repo.get_run_spans("ten_1", run_id, 1) == []
