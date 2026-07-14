from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    jwt_secret: str = "dev-secret-change-me"
    jwt_alg: str = "HS256"
    storage_root: Path = Path("./data")
    orchestrator_url: str = "http://orchestrator-svc:8000"
    signed_url_ttl_sec: int = 600  # §11 署名URL 10分

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            jwt_secret=os.environ.get("JWT_SECRET", "dev-secret-change-me"),
            jwt_alg=os.environ.get("JWT_ALG", "HS256"),
            storage_root=Path(os.environ.get("STORAGE_ROOT", "./data")),
            orchestrator_url=os.environ.get("ORCHESTRATOR_URL", "http://orchestrator-svc:8000"),
        )
