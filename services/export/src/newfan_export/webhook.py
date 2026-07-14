"""Webhook 配信（§6.4 / §11）。

- 署名: X-NF-Signature: sha256=HMAC-SHA256(body, endpoint_secret)、X-NF-Timestamp。
- リトライ: 5回指数（1m/5m/30m/2h/12h）。本モジュールは 1 回送信＋スケジュール算出を提供し、
  実際の再送は §9 のジョブ基盤（q.export のリトライ）に委ねる。
- SSRF 対策（§11）: 宛先がプライベート/ループバック/リンクローカルIPに解決される URL を拒否。
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import socket
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import httpx

from newfan_export.errors import ExportError

# §6.4 の指数バックオフ（秒）
RETRY_SCHEDULE_SEC = [60, 300, 1800, 7200, 43200]

Resolver = Callable[[str], list[str]]


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


def _default_resolver(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, None)
    return [str(info[4][0]) for info in infos]


def is_blocked_url(url: str, *, resolver: Resolver = _default_resolver) -> bool:
    """SSRF ガード: 非 http(s) / 解決先がプライベート等の URL を拒否（§11）。"""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return True
    host = parsed.hostname
    if host in ("localhost",):
        return True

    candidates: list[str]
    try:
        ipaddress.ip_address(host)
        candidates = [host]
    except ValueError:
        try:
            candidates = resolver(host)
        except OSError:
            return True  # 解決不能は拒否

    for addr in candidates:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return True
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return True
    return False


class WebhookSender:
    def __init__(
        self,
        *,
        client: Optional[httpx.Client] = None,
        timeout: float = 10.0,
        resolver: Resolver = _default_resolver,
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
            return False
        return 200 <= resp.status_code < 300
