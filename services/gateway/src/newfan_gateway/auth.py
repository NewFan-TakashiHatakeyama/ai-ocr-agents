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
