"""認証・認可（§6.1 / §11）。JWT（Web UI）と X-API-Key（M2M）。RBAC は階層ランク。"""

from __future__ import annotations

from typing import Optional, Protocol

import jwt
from pydantic import BaseModel

from newfan_gateway.errors import ApiError

# ロール階層（§11）。api は uploader+viewer 相当 → uploader ランク。
ROLE_RANK = {
    "viewer": 1,
    "uploader": 2,
    "api": 2,
    "reviewer": 3,
    "admin": 4,
}


class Principal(BaseModel):
    sub: str
    tenant_id: str
    role: str

    @property
    def rank(self) -> int:
        return ROLE_RANK.get(self.role, 0)


class ApiKeyStore(Protocol):
    def resolve(self, api_key: str) -> Optional[Principal]:
        """API キー → Principal（未知なら None）。"""
        ...


class InMemoryApiKeyStore:
    def __init__(self, keys: dict[str, Principal]) -> None:
        self._keys = keys

    def resolve(self, api_key: str) -> Optional[Principal]:
        return self._keys.get(api_key)


class EnvApiKeyStore:
    """環境変数 API_KEYS(JSON) 由来の API キーストア（本番: Secrets Manager 注入, §11）。

    各要素は {"key","tenant_id","role"?,"sub"?}。キーは SHA-256 ハッシュで保持し、
    平文をメモリに残さず照合する（role 既定 api / sub 既定 m2m）。
    """

    def __init__(self, entries: list[dict[str, str]]) -> None:
        import hashlib

        self._by_hash: dict[str, Principal] = {}
        for e in entries:
            digest = hashlib.sha256(e["key"].encode("utf-8")).hexdigest()
            self._by_hash[digest] = Principal(
                sub=e.get("sub", "m2m"), tenant_id=e["tenant_id"], role=e.get("role", "api")
            )

    def resolve(self, api_key: str) -> Optional[Principal]:
        import hashlib

        return self._by_hash.get(hashlib.sha256(api_key.encode("utf-8")).hexdigest())

    @classmethod
    def from_env(cls, raw: Optional[str] = None) -> "EnvApiKeyStore":
        import json
        import os

        return cls(json.loads(raw if raw is not None else os.environ.get("API_KEYS", "[]")))


def decode_principal(
    *,
    authorization: Optional[str],
    api_key: Optional[str],
    jwt_secret: str,
    jwt_alg: str,
    api_key_store: ApiKeyStore,
) -> Principal:
    if api_key:
        principal = api_key_store.resolve(api_key)
        if principal is None:
            raise ApiError("E5001", "無効な API キーです")
        return principal

    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:]
        try:
            claims = jwt.decode(token, jwt_secret, algorithms=[jwt_alg])
        except jwt.PyJWTError as exc:
            raise ApiError("E5001", "無効なトークンです") from exc
        try:
            return Principal(sub=claims["sub"], tenant_id=claims["tenant_id"], role=claims["role"])
        except KeyError as exc:
            raise ApiError("E5001", "トークンのクレームが不足しています") from exc

    raise ApiError("E5001", "認証情報がありません")


def check_min_role(principal: Principal, min_role: str) -> None:
    if principal.rank < ROLE_RANK[min_role]:
        raise ApiError("E5001", f"権限不足（要 {min_role} 以上）")
