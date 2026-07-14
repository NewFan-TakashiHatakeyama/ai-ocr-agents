from __future__ import annotations

import uuid

_PREFIX = {"correction": "cor", "memory": "mem", "rule": "rul"}


def new_id(kind: str) -> str:
    return f"{_PREFIX.get(kind, kind)}_{uuid.uuid4().hex[:24]}"
