import json
from pathlib import Path

import httpx
from newfan_schemas import ExtractedField

from newfan_export import ExportInput, ExportService, LocalObjectStore, WebhookEndpoint, WebhookSender

_PUBLIC_RESOLVER = lambda host: ["93.184.216.34"]  # noqa: E731


def _input() -> ExportInput:
    return ExportInput(
        tenant_id="ten_1",
        document_id="doc_1",
        run_id="run_1",
        fields=[ExtractedField(name="total_amount", value_normalized="128000")],
    )


def test_export_saves_json_and_delivers(tmp_path: Path) -> None:
    delivered: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        delivered["event"] = json.loads(request.content)
        return httpx.Response(200)

    sender = WebhookSender(
        client=httpx.Client(transport=httpx.MockTransport(handler)), resolver=_PUBLIC_RESOLVER
    )
    svc = ExportService(LocalObjectStore(tmp_path), sender)

    result = svc.export_confirmed(
        _input(), [WebhookEndpoint(url="https://example.com/hook", secret="s")]
    )

    # canonical JSON が保存されている
    saved = tmp_path / "ten_1/doc_1/derived/run_1.json"
    assert saved.exists()
    doc = json.loads(saved.read_text(encoding="utf-8"))
    assert doc["fields"][0]["final"] == "128000"

    # webhook 配信済み
    assert result.deliveries[0].delivered is True
    assert delivered["event"]["event"] == "document.confirmed"


def test_export_records_ssrf_block(tmp_path: Path) -> None:
    sender = WebhookSender(client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))))
    svc = ExportService(LocalObjectStore(tmp_path), sender)
    result = svc.export_confirmed(
        _input(), [WebhookEndpoint(url="http://127.0.0.1/hook", secret="s")]
    )
    assert result.deliveries[0].delivered is False
    assert result.deliveries[0].error == "E5001"
    # JSON は保存される（配信失敗でも成果物は残す）
    assert (tmp_path / "ten_1/doc_1/derived/run_1.json").exists()
