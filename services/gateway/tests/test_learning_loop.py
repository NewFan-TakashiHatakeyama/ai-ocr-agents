"""confirm が保存済み修正を feedback としてグラフへ渡すこと（§3.2 / DD-06）。

以前は confirm が body.overrides しか resume に渡しておらず、修正は correction_logs に
記録されるのに learn ノードへ何も届かず、実 AWS で tenant_memories が 0 件のまま
confirmed になっていた（学習ループが成立していなかった）。
"""

from __future__ import annotations

from types import SimpleNamespace

from gw_helpers import PDF, auth


def _upload(ctx: SimpleNamespace) -> str:
    r = ctx.client.post(
        "/v1/documents",
        files={"file": ("invoice.pdf", PDF, "application/pdf")},
        headers=auth("uploader"),
    )
    assert r.status_code == 201
    return str(r.json()["document_id"])


def _extract(ctx: SimpleNamespace, doc_id: str) -> str:
    r = ctx.client.post(f"/v1/documents/{doc_id}/extract", json={}, headers=auth("uploader"))
    assert r.status_code == 202
    return str(r.json()["run_id"])


def test_confirm_passes_saved_corrections_to_graph(ctx: SimpleNamespace) -> None:
    doc_id = _upload(ctx)
    run_id = _extract(ctx, doc_id)

    r = ctx.client.post(
        f"/v1/documents/{doc_id}/corrections",
        json={
            "run_id": run_id,
            "version": 1,
            "items": [
                {
                    "field_name": "取引先名",
                    "original_value": "株式会社エイ化ーエム",
                    "corrected_value": "株式会社エイビーエム",
                    "supplier_key": "わくわく物産株式会社",
                    "context": "株式会社エイ化ーエム 総務部",
                }
            ],
        },
        headers=auth("reviewer"),
    )
    assert r.status_code == 200

    assert ctx.client.post(
        f"/v1/documents/{doc_id}/confirm", json={"run_id": run_id}, headers=auth("reviewer")
    ).status_code == 202

    # FakeOrchestratorClient が resume で受け取った feedback を検証する
    assert ctx.orch.resumed, "resume が呼ばれていない"
    _run_id, _tenant, feedback = ctx.orch.resumed[-1]
    corrections = (feedback or {}).get("corrections") or []
    assert len(corrections) == 1, "保存済み修正が feedback に載っていない"
    c = corrections[0]
    assert c["field_name"] == "取引先名"
    assert c["corrected_value"] == "株式会社エイビーエム"
    # learn が memory へ渡す検索キー / embedding 入力（DD-06/DD-07）
    assert c["supplier_key"] == "わくわく物産株式会社"
    assert c["context"] == "株式会社エイ化ーエム 総務部"


def test_confirm_without_corrections_sends_empty_list(ctx: SimpleNamespace) -> None:
    """修正なしの確定でも resume は呼ぶ（全項目 approved で finalize させる）。"""
    doc_id = _upload(ctx)
    run_id = _extract(ctx, doc_id)
    assert ctx.client.post(
        f"/v1/documents/{doc_id}/confirm", json={"run_id": run_id}, headers=auth("reviewer")
    ).status_code == 202
    _r, _t, feedback = ctx.orch.resumed[-1]
    assert (feedback or {}).get("corrections") == []
