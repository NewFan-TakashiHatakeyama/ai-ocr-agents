"""ExportWorker 単体テスト（§9）。source/exporter/consumer は Fake。"""

from __future__ import annotations

from typing import Any, Optional

from newfan_export.models import ExportInput, WebhookEndpoint
from newfan_export.worker import ExportWorker
from newfan_schemas import ExtractedField


class _FakeSource:
    def __init__(self, inp: Optional[ExportInput], endpoints: list[WebhookEndpoint]) -> None:
        self._inp = inp
        self._endpoints = endpoints

    def load_export_input(self, tenant_id: str, run_id: str) -> Optional[ExportInput]:
        return self._inp

    def list_webhook_endpoints(self, tenant_id: str) -> list[WebhookEndpoint]:
        return self._endpoints


class _FakeExporter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def export_confirmed(self, inp: ExportInput, endpoints: list[WebhookEndpoint]) -> Any:
        self.calls.append((inp.run_id, len(endpoints)))
        return None


class _FakeConsumer:
    def __init__(self) -> None:
        self._pending: list[tuple[str, dict[str, Any]]] = []
        self.acked: list[str] = []
        self._seq = 0

    def push(self, payload: dict[str, Any]) -> None:
        self._seq += 1
        self._pending.append((f"m{self._seq}", payload))

    def consume(self, *, count: int = 10, block_ms: int = 0) -> list[tuple[str, dict[str, Any]]]:
        out, self._pending = self._pending[:count], self._pending[count:]
        return out

    def ack(self, message_id: str) -> None:
        self.acked.append(message_id)


def _inp() -> ExportInput:
    return ExportInput(
        tenant_id="ten_1", document_id="doc_1", run_id="run_1",
        fields=[ExtractedField(name="total_amount", value_normalized="128000")],
    )


def test_process_exports_with_endpoints() -> None:
    exporter = _FakeExporter()
    worker = ExportWorker(
        _FakeSource(_inp(), [WebhookEndpoint(url="https://x.example/hook", secret="s")]),
        exporter,
        _FakeConsumer(),
    )
    assert worker.process({"tenant_id": "ten_1", "run_id": "run_1"}) == "exported"
    assert exporter.calls == [("run_1", 1)]


def test_process_skips_when_run_missing() -> None:
    exporter = _FakeExporter()
    worker = ExportWorker(_FakeSource(None, []), exporter, _FakeConsumer())
    assert worker.process({"tenant_id": "ten_1", "run_id": "gone"}) == "skipped"
    assert exporter.calls == []


def test_run_once_consumes_and_acks() -> None:
    exporter = _FakeExporter()
    consumer = _FakeConsumer()
    consumer.push({"tenant_id": "ten_1", "run_id": "run_1"})
    worker = ExportWorker(_FakeSource(_inp(), []), exporter, consumer)
    assert worker.run_once() == 1
    assert consumer.acked == ["m1"]
    assert exporter.calls == [("run_1", 0)]
