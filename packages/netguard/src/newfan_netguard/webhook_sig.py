"""Webhook の署名とヘッダ（§6.4）。

もとは newfan_export.webhook にあったが、gateway の疎通テスト（POST /connections/{id}/test）
も**本配信と同じ署名・同じヘッダ**で送る必要がある。受信側は署名検証をテストで通した
上で有効化するため、ここが本配信と少しでもずれると「テストは通るのに本番が弾かれる」
（またはその逆）になる。gateway → export の import は層が逆転するため、SSRF ガードと
同じくここ（標準ライブラリのみ）に置き、両者が同じ関数を呼ぶ。

- 本文: JSON（ensure_ascii=False, 区切り最小）を UTF-8 で符号化したバイト列
- X-NF-Signature: sha256=HMAC-SHA256(body, secret) の hex
- X-NF-Timestamp: 送信時刻（epoch 秒）
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any, Optional

SIGNATURE_HEADER = "X-NF-Signature"
TIMESTAMP_HEADER = "X-NF-Timestamp"


def sign(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def encode_event(event: dict[str, Any]) -> bytes:
    """署名対象の本文。配信側と受信側の検証が同じバイト列を見るよう、ここで一意に決める。"""
    return json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def signed_headers(body: bytes, secret: str, *, now: Optional[float] = None) -> dict[str, str]:
    """本配信と疎通テストが共有するリクエストヘッダ。"""
    return {
        "Content-Type": "application/json",
        SIGNATURE_HEADER: sign(body, secret),
        TIMESTAMP_HEADER: str(int(now if now is not None else time.time())),
    }
