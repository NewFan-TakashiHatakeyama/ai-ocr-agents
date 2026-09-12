"""POST /documents/{id}/classify（⑦ 抽出UIサジェスト）。"""

from types import SimpleNamespace

from gw_helpers import PDF, auth

from newfan_gateway.records import RunRecord, SchemaFieldDef, SchemaRecord


def _upload(ctx: SimpleNamespace, filename: str, doc_type: str | None = None) -> str:
    data = {"doc_type": doc_type} if doc_type else {}
    r = ctx.client.post(
        "/v1/documents",
        headers=auth("uploader"),
        files={"file": (filename, PDF, "application/pdf")},
        data=data,
    )
    assert r.status_code == 201, r.text
    return r.json()["document_id"]


def test_classify_declared_doc_typeを最優先する(ctx: SimpleNamespace) -> None:
    doc_id = _upload(ctx, "scan.pdf", doc_type="invoice")
    r = ctx.client.post(f"/v1/documents/{doc_id}/classify", headers=auth("viewer"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["doc_type"] == "invoice"
    assert body["method"] == "declared"
    assert body["suggested_schema_id"]
    assert body["confidence"] == 1.0


def test_classify_ファイル名から種別を推定する(ctx: SimpleNamespace) -> None:
    # doc_type 未指定・ファイル名に請求書 → invoice を提案（seed 済スキーマは invoice）
    doc_id = _upload(ctx, "請求書_ABC商事_2026.pdf")
    r = ctx.client.post(f"/v1/documents/{doc_id}/classify", headers=auth("viewer"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["doc_type"] == "invoice"
    assert body["method"] == "filename"
    assert body["confidence"] > 0.0
    assert body["suggested_schema_id"]


def test_classify_手がかりなしはサジェストしない(ctx: SimpleNamespace) -> None:
    doc_id = _upload(ctx, "0001.pdf")
    r = ctx.client.post(f"/v1/documents/{doc_id}/classify", headers=auth("viewer"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["suggested_schema_id"] is None
    assert body["doc_type"] is None


# ---- 表題部の本文信号（ADR-0008）。ページは FakeRasterizer の 1000×1400（上端 25% = 350px）----


def _seed_quotation_schema(ctx: SimpleNamespace) -> None:
    ctx.admin.seed_schema(
        SchemaRecord(
            id="sch_q",
            tenant_id="ten_1",
            doc_type="quotation",
            version=1,
            fields=[
                SchemaFieldDef(name="quote_no", label="見積番号", type="string"),
                SchemaFieldDef(name="total", label="御見積合計金額", type="money_jpy"),
            ],
        )
    )


def _seed_run(ctx: SimpleNamespace, doc_id: str, spans: list[dict], run_id: str = "run_t1") -> None:
    ctx.repo.create_run(
        RunRecord(id=run_id, tenant_id="ten_1", document_id=doc_id, status="needs_review")
    )
    if spans:
        ctx.repo.seed_run_spans(run_id, 1, spans)


def _classify(ctx: SimpleNamespace, doc_id: str) -> dict:
    r = ctx.client.post(f"/v1/documents/{doc_id}/classify", headers=auth("viewer"))
    assert r.status_code == 200, r.text
    return r.json()


def _scores(body: dict) -> dict[str, float]:
    return {c["doc_type"]: c["score"] for c in body["candidates"]}


def test_classify_抽出済みなら表題部で種別を当てる(ctx: SimpleNamespace) -> None:
    # ファイル名に手がかりが無い帳票でも、表題「御請求書」から invoice を提案する
    doc_id = _upload(ctx, "0001.pdf")
    _seed_run(ctx, doc_id, [{"span_id": 1, "text": "御請求書", "bbox": [400, 60, 600, 110], "conf": 0.99}])
    body = _classify(ctx, doc_id)
    assert body["doc_type"] == "invoice"
    assert body["suggested_schema_id"] == "sch_1"
    assert body["method"] == "filename+title"
    assert body["confidence"] == 0.667  # 表題部 1 領域（ゲート閾値 0.75 未満の弱い提案）
    assert body["reason"] == "「請求書・御請求」が表題部に一致"


def test_classify_明細部の見積語では請求書が見積に反転しない(ctx: SimpleNamespace) -> None:
    # 請求書の明細（350px より下）が見積を 3 箇所で引用する。本文全体を信号にすると
    # quotation 3.0 vs invoice 1.0 で反転する（packages/workflow のテストで固定）。
    # 表題部だけを見るので quotation は 0 のまま。
    _seed_quotation_schema(ctx)
    doc_id = _upload(ctx, "scan_0001.pdf")
    _seed_run(ctx, doc_id, [
        {"span_id": 1, "text": "請求書", "bbox": [400, 60, 600, 110], "conf": 0.99},
        {"span_id": 2, "text": "株式会社ABC 御中", "bbox": [50, 150, 350, 180], "conf": 0.9},
        {"span_id": 3, "text": "請求No. 1234", "bbox": [700, 150, 950, 180], "conf": 0.9},
        {"span_id": 4, "text": "見積番号 Q-1 に基づく", "bbox": [50, 600, 400, 630], "conf": 0.9},
        {"span_id": 5, "text": "見積金額 ¥100,000", "bbox": [50, 640, 400, 670], "conf": 0.9},
        {"span_id": 6, "text": "見積書 No.2 参照", "bbox": [50, 680, 400, 710], "conf": 0.9},
    ])
    body = _classify(ctx, doc_id)
    assert body["doc_type"] == "invoice"
    assert body["suggested_schema_id"] == "sch_1"
    assert body["method"] == "filename+title"
    assert _scores(body) == {"invoice": 1.0, "quotation": 0.0}
    assert body["confidence"] == 0.667


def test_classify_表題が食い違ってもファイル名が勝ち_確信度は下がる(ctx: SimpleNamespace) -> None:
    # ファイル名は請求書（3.0）、表題は「御見積書」（1.0）。quotation は 0 → 1.0 に上がるが
    # invoice が勝つ。確信度は 1.0 → 0.75（表題部が寄与したので method は filename+title）。
    _seed_quotation_schema(ctx)
    doc_id = _upload(ctx, "請求書_ABC商事_2026.pdf")
    before = _classify(ctx, doc_id)
    assert (before["doc_type"], before["confidence"], before["method"]) == ("invoice", 1.0, "filename")
    _seed_run(ctx, doc_id, [{"span_id": 1, "text": "御見積書", "bbox": [400, 60, 600, 110], "conf": 0.99}])
    body = _classify(ctx, doc_id)
    assert body["doc_type"] == "invoice"
    assert body["suggested_schema_id"] == "sch_1"
    assert body["method"] == "filename+title"
    assert body["confidence"] == 0.75
    assert _scores(body) == {"invoice": 3.0, "quotation": 1.0}
    assert body["reason"] == "「請求書」がファイル名に一致"


def test_classify_spanの無いrunは従来どおりファイル名だけ(ctx: SimpleNamespace) -> None:
    # 0008 より前の run・失敗 run・実行中の run: run_spans の行が無い → method は filename
    doc_id = _upload(ctx, "請求書_ABC商事_2026.pdf")
    _seed_run(ctx, doc_id, [])
    body = _classify(ctx, doc_id)
    assert (body["doc_type"], body["confidence"], body["method"]) == ("invoice", 1.0, "filename")


def test_classify_ページ寸法が無ければ表題部を使わない(ctx: SimpleNamespace) -> None:
    # pages.height は nullable。寸法不明では「上端」を決められないのでファイル名のみ（fail-open）
    doc_id = _upload(ctx, "0001.pdf")
    _seed_run(ctx, doc_id, [{"span_id": 1, "text": "御請求書", "bbox": [400, 60, 600, 110], "conf": 0.99}])
    for p in ctx.repo._pages[doc_id]:  # noqa: SLF001
        p.height = None
    body = _classify(ctx, doc_id)
    assert body["doc_type"] is None
    assert body["method"] == "filename"


def test_classify_declaredは表題部より優先される(ctx: SimpleNamespace) -> None:
    doc_id = _upload(ctx, "0001.pdf", doc_type="invoice")
    _seed_quotation_schema(ctx)
    _seed_run(ctx, doc_id, [{"span_id": 1, "text": "御見積書", "bbox": [400, 60, 600, 110], "conf": 0.99}])
    body = _classify(ctx, doc_id)
    assert (body["doc_type"], body["method"], body["confidence"]) == ("invoice", "declared", 1.0)
