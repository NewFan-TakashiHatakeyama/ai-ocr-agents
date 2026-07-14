from __future__ import annotations

import uuid

# ID 規約: ULID を想定（§7）。ここでは uuid4 で近似し、プレフィクスを付す。
_PREFIX = {
    "tenant": "ten",
    "user": "usr",
    "document": "doc",
    "run": "run",
    "field": "fld",
    "correction": "cor",
    "rule": "rul",
    "job": "job",
    "schema": "sch",
    "request": "req",
}


def new_id(kind: str) -> str:
    prefix = _PREFIX.get(kind, kind)
    return f"{prefix}_{uuid.uuid4().hex[:24]}"
