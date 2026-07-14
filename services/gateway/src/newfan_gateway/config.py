from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _default_cors() -> list[str]:
    return ["http://localhost:3000"]


@dataclass(frozen=True)
class Settings:
    jwt_secret: str = "dev-secret-change-me"
    jwt_alg: str = "HS256"
    storage_root: Path = Path("./data")
    orchestrator_url: str = "http://orchestrator-svc:8000"
    signed_url_ttl_sec: int = 600  # §11 署名URL 10分
    cors_origins: list[str] = field(default_factory=_default_cors)
    database_url: str | None = None  # 未設定なら InMemoryRepository（開発/テスト）
    redis_url: str | None = None  # 未設定なら InMemoryQueue（開発/テスト）

    @classmethod
    def from_env(cls) -> "Settings":
        origins = os.environ.get("CORS_ORIGINS")
        return cls(
            jwt_secret=os.environ.get("JWT_SECRET", "dev-secret-change-me"),
            jwt_alg=os.environ.get("JWT_ALG", "HS256"),
            storage_root=Path(os.environ.get("STORAGE_ROOT", "./data")),
            orchestrator_url=os.environ.get("ORCHESTRATOR_URL", "http://orchestrator-svc:8000"),
            cors_origins=(
                [o.strip() for o in origins.split(",")] if origins else _default_cors()
            ),
            database_url=os.environ.get("DATABASE_URL"),
            redis_url=os.environ.get("REDIS_URL"),
        )
