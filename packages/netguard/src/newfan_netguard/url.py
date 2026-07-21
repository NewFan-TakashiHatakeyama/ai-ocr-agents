"""URL の SSRF 判定（§11）。

もとは newfan_export.webhook にあったが、gateway（配信先の登録）でも同じ判定が要る。
gateway → export の import は層が逆転するため、共有パッケージへ移した。
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Callable
from urllib.parse import urlparse

Resolver = Callable[[str], list[str]]


def default_resolver(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, None)
    return [str(info[4][0]) for info in infos]


def is_blocked_url(url: str, *, resolver: Resolver = default_resolver) -> bool:
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
