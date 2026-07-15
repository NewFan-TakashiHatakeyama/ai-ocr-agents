"""PgContextStore が schema 未割当の run に渡す placeholder の契約（§4.3 / §7）。

schema_id IS NULL（テンプレートレス既定）の run で doc_type=None を渡すと
deterministic_normalize の FieldSchema.model_validate が ValidationError で落ち、
worker は ACK せず無言で再配信し続ける。実コンテナの E2E で踏んだ回帰を固定する。
"""

from __future__ import annotations

import pytest

from newfan_schemas.field_schema import FieldSchema

pytest.importorskip("sqlalchemy", reason="PgContextStore は runtime 依存")

from newfan_orchestrator.pg_persistence import EMPTY_SCHEMA  # noqa: E402


def test_empty_schema_is_a_valid_field_schema() -> None:
    schema = FieldSchema.model_validate(dict(EMPTY_SCHEMA))
    assert schema.doc_type == ""
    assert schema.fields == []


def test_empty_schema_matches_nodes_default() -> None:
    """nodes 側の既定リテラルと同一。片方だけ None に戻る差分を防ぐ。"""
    assert EMPTY_SCHEMA == {"doc_type": "", "fields": []}
