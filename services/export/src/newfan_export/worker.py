"""export-svc ワーカー（§5.9 / §9）。q.export を消費し確定データを配信する。

外部境界（DB=source / 保存・配信=service / キュー=consumer）は Protocol 依存。
テストは Fake、本番は PgExportSource + ExportService + RedisStreamConsumer を注入する。
"""

from __future__ import annotations

from typing import Any, Optional, Protocol

from newfan_export.models import ExportInput, WebhookEndpoint


class ExportSource(Protocol):
    def load_export_input(self, tenant_id: str, run_id: str) -> Optional[ExportInput]: ...
    def list_webhook_endpoints(self, tenant_id: str) -> list[WebhookEndpoint]: ...


class Exporter(Protocol):
    def export_confirmed(self, inp: ExportInput, endpoints: list[WebhookEndpoint]) -> Any: ...


class QueueConsumer(Protocol):
    def consume(self, *, count: int = ..., block_ms: int = ...) -> list[tuple[str, dict[str, Any]]]: ...
    def ack(self, message_id: str) -> None: ...


class ExportWorker:
    def __init__(self, source: ExportSource, exporter: Exporter, consumer: QueueConsumer) -> None:
        self._source = source
        self._exporter = exporter
        self._consumer = consumer

    def process(self, payload: dict[str, Any]) -> str:
        """1 ジョブを処理して結果を返す（'exported' / 'skipped'）。"""
        tenant_id = payload["tenant_id"]
        run_id = payload["run_id"]
        inp = self._source.load_export_input(tenant_id, run_id)
        if inp is None:  # 確定 Run が無い（再配信・競合）→ skip（ack して破棄）
            return "skipped"
        endpoints = self._source.list_webhook_endpoints(tenant_id)
        self._exporter.export_confirmed(inp, endpoints)
        return "exported"

    def run_once(self, *, count: int = 10) -> int:
        """キューにあるジョブをまとめて処理する。失敗は ACK せず再配信に委ねる（§9）。"""
        processed = 0
        for message_id, payload in self._consumer.consume(count=count):
            try:
                self.process(payload)
                self._consumer.ack(message_id)
                processed += 1
            except Exception:  # noqa: BLE001 - 失敗は ACK せず再配信
                continue
        return processed
