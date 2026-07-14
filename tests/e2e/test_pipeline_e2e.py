"""E2E: ingest → 抽出グラフ(§4) → export(§5.9) を実 LangGraph で一気通し。

外部境界（GPU サービング / クラウド LLM）のみ Fake：
- structure-svc: FakeStructureClient が /layout-parsing 応答を返す（GPU 不要）
- LLM: FakeProvider が KIE の JSON を返す（API キー不要）
それ以外（ingest 検証・span 構築・正規化・confidence・validate・gate・memory・export）は実装本体。
実サービングに差し替える手順は scripts/record_fixtures.py と deploy/compose.yaml 参照。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

pytest.importorskip("langgraph.graph")  # 抽出グラフの実体化に必要（dev group）

from newfan_export import ExportInput, ExportService, LocalObjectStore, WebhookEndpoint, WebhookSender
from newfan_ingest import IngestService, UploadInput
from newfan_ingest.rasterize import RasterPage
from newfan_ingest.storage import LocalObjectStore as IngestLocalStore
from newfan_llm_adapter import FakeProvider, LLMAdapter, PromptBundle, default_bundle_dir
from newfan_memory import HashingEmbedder, InMemoryMemoryRepository, MemoryService
from newfan_paddle_client import LayoutParsingResponse
from newfan_orchestrator.graph import build_graph

_PUBLIC_RESOLVER = lambda host: ["93.184.216.34"]  # noqa: E731

# structure-svc の疑似応答（1 span "128000" conf 0.99）
_LAYOUT = {
    "layoutParsingResults": [
        {
            "prunedResult": {
                "parsing_res_list": [
                    {
                        "block_bbox": [40, 40, 520, 90],
                        "block_label": "text",
                        "block_content": "御請求金額 128000",
                        "block_id": 0,
                        "block_order": 0,
                    }
                ],
                "overall_ocr_res": {
                    "rec_texts": ["128000"],
                    "rec_scores": [0.99],
                    "rec_polys": [[[300, 180], [430, 180], [430, 212], [300, 212]]],
                },
            },
            "markdown": {"text": "# 請求書\n御請求金額 128000", "isStart": True, "isEnd": True},
        }
    ]
}

_KIE_RESPONSE = json.dumps(
    {
        "fields": [{"name": "total_amount", "value": "128000", "span_ids": [0], "page": 1}],
        "tables": [],
        "unmapped_required": [],
    }
)

_SCHEMA = {
    "doc_type": "invoice",
    "fields": [{"name": "total_amount", "type": "money_jpy", "critical": True}],
}


class _FakeStructureClient:
    def layout_parsing(self, file_b64: str, *, file_type: int = 1) -> LayoutParsingResponse:
        return LayoutParsingResponse.model_validate(_LAYOUT)


class _FakeRasterizer:
    def rasterize(self, content: bytes, kind: str) -> list[RasterPage]:
        return [RasterPage(page_no=1, width=1000, height=1400, png_bytes=b"\x89PNG-page-1")]


def test_ingest_to_export_pipeline(tmp_path: Path) -> None:
    # 1. ingest（実検証・ページ分割・前処理メタ）
    ingestor = IngestService(IngestLocalStore(tmp_path), _FakeRasterizer())
    ingest_result = ingestor.ingest(
        UploadInput(
            tenant_id="ten_1", document_id="doc_1", filename="invoice.pdf", content=b"%PDF-1.7\nx"
        )
    )
    assert ingest_result.page_count == 1
    pages = [
        {"page_no": p.page_no, "image_uri": p.image_uri, "width": p.width, "height": p.height}
        for p in ingest_result.pages
    ]

    # 2. 抽出グラフを実体化（structure_ocr=Fake, kie/correct=FakeLLM, memory=実装）
    adapter = LLMAdapter(FakeProvider([_KIE_RESPONSE]))
    bundle = PromptBundle.load(default_bundle_dir())
    memory = MemoryService(HashingEmbedder(), InMemoryMemoryRepository())
    graph = build_graph(
        adapter=adapter, bundle=bundle, memory=memory, structure_client=_FakeStructureClient()
    )

    # 3. 実行（自動確定経路：高確信・grounding 一致で pending なし）
    initial: dict[str, Any] = {
        "run_id": "run_1",
        "document_id": "doc_1",
        "tenant_id": "ten_1",
        "schema": _SCHEMA,
        "pages": pages,
    }
    final = graph.invoke(initial, config={"configurable": {"thread_id": "run_1"}})

    fields = final["fields"]
    total = next(f for f in fields if f.name == "total_amount")
    assert total.value_normalized == "128000"
    assert total.confidence >= 0.90  # critical 閾値を超え自動確定
    assert total.grounding_score == 1.0  # span 原文と一致
    assert not final.get("review_items")  # レビュー要なし → finalize 経路

    # 4. export（canonical JSON 保存 ＋ Webhook 配信）
    delivered: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        delivered["event"] = json.loads(request.content)
        return httpx.Response(200)

    sender = WebhookSender(
        client=httpx.Client(transport=httpx.MockTransport(handler)), resolver=_PUBLIC_RESOLVER
    )
    export_svc = ExportService(LocalObjectStore(tmp_path / "export"), sender)
    result = export_svc.export_confirmed(
        ExportInput(
            tenant_id="ten_1",
            document_id="doc_1",
            run_id="run_1",
            fields=fields,
            tables=final.get("tables", []),
            engine_versions={"paddleocr": "3.7.0"},
        ),
        [WebhookEndpoint(url="https://example.com/hook", secret="s")],
    )

    # canonical JSON が保存され、確定値が入っている
    saved = tmp_path / "export/ten_1/doc_1/derived/run_1.json"
    assert saved.exists()
    canonical = json.loads(saved.read_text(encoding="utf-8"))
    assert next(f for f in canonical["fields"] if f["name"] == "total_amount")["final"] == "128000"
    # webhook 配信済み
    assert result.deliveries[0].delivered is True
    assert delivered["event"]["event"] == "document.confirmed"
