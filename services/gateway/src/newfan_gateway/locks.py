"""検証画面のソフトロック（協調的・TTL 付き, §8.2）。

同一帳票を複数レビュアが同時に編集して確定が衝突するのを緩和する助言的ロック。
強制ではなく、他者が保持中なら UI を読取専用にして注意喚起する（§6.3 の楽観ロックは
最終防衛線）。TTL でクライアント異常終了時も自動解放する。

本番は Redis 等の共有ストアに差し替え可能なよう Protocol で抽象化する。MVP は
プロセス内 InMemory（idempotency ストアと同格）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional, Protocol

from pydantic import BaseModel

DEFAULT_TTL_SEC = 300  # 5 分（§8.2）


def _now() -> datetime:
    return datetime.now(timezone.utc)


class LockInfo(BaseModel):
    tenant_id: str
    document_id: str
    holder_sub: str  # ロック保持者の principal.sub
    holder_name: str  # 表示名（当面 sub と同値）
    acquired_at: datetime
    expires_at: datetime

    def is_expired(self, at: Optional[datetime] = None) -> bool:
        return (at or _now()) >= self.expires_at

    def remaining_sec(self, at: Optional[datetime] = None) -> int:
        return max(0, int((self.expires_at - (at or _now())).total_seconds()))


class LockStore(Protocol):
    def acquire(
        self, tenant_id: str, document_id: str, holder_sub: str, *, ttl_sec: int = DEFAULT_TTL_SEC
    ) -> tuple[bool, LockInfo]:
        """ロック取得を試みる。

        戻り値 (acquired, info):
        - acquired=True  … 呼び出し元が保持（新規取得 / 自身の更新 / 期限切れの奪取）
        - acquired=False … 他者が保持中（info は現保持者。UI は読取専用にする）
        """
        ...

    def release(self, tenant_id: str, document_id: str, holder_sub: str) -> bool:
        """保持者本人による解放。解放できたら True（他者保持/未ロックは False）。"""
        ...

    def get(self, tenant_id: str, document_id: str) -> Optional[LockInfo]:
        """有効なロックを返す（無い/期限切れは None）。"""
        ...


class InMemoryLockStore:
    def __init__(self) -> None:
        self._locks: dict[tuple[str, str], LockInfo] = {}

    def acquire(
        self, tenant_id: str, document_id: str, holder_sub: str, *, ttl_sec: int = DEFAULT_TTL_SEC
    ) -> tuple[bool, LockInfo]:
        now = _now()
        key = (tenant_id, document_id)
        cur = self._locks.get(key)
        if cur is not None and not cur.is_expired(now) and cur.holder_sub != holder_sub:
            return False, cur
        info = LockInfo(
            tenant_id=tenant_id,
            document_id=document_id,
            holder_sub=holder_sub,
            holder_name=holder_sub,
            acquired_at=now,
            expires_at=now + timedelta(seconds=ttl_sec),
        )
        self._locks[key] = info
        return True, info

    def release(self, tenant_id: str, document_id: str, holder_sub: str) -> bool:
        key = (tenant_id, document_id)
        cur = self._locks.get(key)
        if cur is not None and not cur.is_expired() and cur.holder_sub == holder_sub:
            del self._locks[key]
            return True
        return False

    def get(self, tenant_id: str, document_id: str) -> Optional[LockInfo]:
        key = (tenant_id, document_id)
        cur = self._locks.get(key)
        if cur is None:
            return None
        if cur.is_expired():
            del self._locks[key]
            return None
        return cur
