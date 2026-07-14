"""ExportService: 確定データの JSON 保存 ＋ Webhook 配信（§5.9 / §6.4）。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from newfan_export.canonical import build_canonical_json
from newfan_export.errors import ExportError
from newfan_export.models import ExportInput, WebhookEndpoint
from newfan_export.storage import ObjectStore, canonical_key
from newfan_export.webhook import WebhookSender, build_event


@dataclass
class DeliveryOutcome:
    url: str
    delivered: bool
    error: str | None = None


@dataclass
class ExportResult:
    canonical_uri: str
    deliveries: list[DeliveryOutcome] = field(default_factory=list)


class ExportService:
    def __init__(self, store: ObjectStore, sender: WebhookSender) -> None:
        self._store = store
        self._sender = sender

    def export_confirmed(
        self, inp: ExportInput, endpoints: list[WebhookEndpoint]
    ) -> ExportResult:
        canonical = build_canonical_json(inp)
        body = json.dumps(canonical, ensure_ascii=False, indent=2).encode("utf-8")
        uri = self._store.put(
            canonical_key(inp.tenant_id, inp.document_id, inp.run_id),
            body,
            "application/json",
        )

        event = build_event(
            "document.confirmed",
            tenant_id=inp.tenant_id,
            document_id=inp.document_id,
            run_id=inp.run_id,
            data={
                "status": inp.status,
                "field_count": len(inp.fields),
                "canonical_uri": uri,
            },
        )

        result = ExportResult(canonical_uri=uri)
        for ep in endpoints:
            try:
                ok = self._sender.send(ep.url, ep.secret, event)
                result.deliveries.append(DeliveryOutcome(url=ep.url, delivered=ok))
            except ExportError as exc:
                # ブロック URL 等は配信失敗として記録（再送はしない）
                result.deliveries.append(
                    DeliveryOutcome(url=ep.url, delivered=False, error=exc.code)
                )
        return result
