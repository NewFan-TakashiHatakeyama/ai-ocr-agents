"""Webhook 配信（§6.4 / §11）。

- 署名: X-NF-Signature: sha256=HMAC-SHA256(body, endpoint_secret)、X-NF-Timestamp。
- リトライ: 5回指数（1m/5m/30m/2h/12h）。本モジュールは 1 回送信＋スケジュール算出を提供し、
  実際の再送は §9 のジョブ基盤（q.export のリトライ）に委ねる。
- SSRF 対策（§11）: 宛先がプライベート/ループバック/リンクローカルIPに解決される URL を拒否。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from newfan_metrics import current_tenant, webhook_delivery_failures_total

# SSRF ガードの実体は newfan-netguard。gateway（配信先の登録時）も同じ判定を使うが、
# gateway が export を import すると層が逆転するため共有パッケージへ置いた。
# ここは既存の import（newfan_export.webhook.is_blocked_url）を保つ再エクスポート。
from newfan_netguard import Resolver, default_resolver, is_blocked_url

from newfan_export.errors import ExportError

__all__ = [
    "RETRY_SCHEDULE_SEC",
    "Resolver",
    "WebhookSender",
    "build_event",
    "is_blocked_url",
    "next_retry_delay",
    "sign",
]

# §6.4 の指数バックオフ（秒）
RETRY_SCHEDULE_SEC = [60, 300, 1800, 7200, 43200]


def next_retry_delay(attempt: int) -> Optional[int]:
    """attempt(0始まり) の次リトライまでの秒。枯渇時は None。"""
    if 0 <= attempt < len(RETRY_SCHEDULE_SEC):
        return RETRY_SCHEDULE_SEC[attempt]
    return None


def sign(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def build_event(
    event: str,
    *,
    tenant_id: str,
    document_id: str,
    run_id: str,
    data: dict[str, Any],
    occurred_at: Optional[str] = None,
) -> dict[str, Any]:
    return {
        "event": event,
        "tenant_id": tenant_id,
        "document_id": document_id,
        "run_id": run_id,
        "occurred_at": occurred_at or datetime.now(timezone.utc).isoformat(),
        "data": data,
    }


class WebhookSender:
    def __init__(
        self,
        *,
        client: Optional[httpx.Client] = None,
        timeout: float = 10.0,
        resolver: Resolver = default_resolver,
    ) -> None:
        self._client = client or httpx.Client(timeout=timeout)
        self._resolver = resolver

    def send(self, url: str, secret: str, event: dict[str, Any]) -> bool:
        """1 回送信して成否を返す。ブロック URL は ExportError。"""
        if is_blocked_url(url, resolver=self._resolver):
            raise ExportError("E5001", f"配信先 URL が拒否されました: {url}")
        body = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "X-NF-Signature": sign(body, secret),
            "X-NF-Timestamp": str(int(time.time())),
        }
        try:
            resp = self._client.post(url, content=body, headers=headers)
        except httpx.HTTPError:
            # §12.1 webhook_delivery_failures_total。§12.3 は「webhook 失敗率>10%」で
            # アラートするので、ネットワーク例外も HTTP エラーも同じく失敗として数える。
            webhook_delivery_failures_total.labels(tenant=current_tenant()).inc()
            return False
        ok = 200 <= resp.status_code < 300
        if not ok:
            webhook_delivery_failures_total.labels(tenant=current_tenant()).inc()
        return ok
