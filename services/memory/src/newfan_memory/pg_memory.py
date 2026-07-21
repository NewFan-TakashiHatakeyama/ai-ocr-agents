# mypy: ignore-errors
"""MemoryRepository の PostgreSQL 実装（正本, §5.8.3 / §7.2）。

correction_logs / tenant_memories / tenant_rules を正本として永続化する。
FAISS index は MemoryService 側で list_memories からプロセス毎に再構築される。
RLS 用に set_config('app.tenant_id', ...) を発行（§7.3）。runtime 依存（sqlalchemy + psycopg）。
"""

from __future__ import annotations

import json
from typing import Optional

from sqlalchemy import create_engine, text

from newfan_memory.records import (
    CorrectionLog,
    RuleStatus,
    RuleType,
    TenantMemory,
    TenantRule,
)


def _correction(row) -> CorrectionLog:
    return CorrectionLog(
        id=row.id,
        tenant_id=row.tenant_id,
        document_id=row.document_id,
        run_id=row.run_id,
        field_name=row.field_name,
        original_value=row.original_value,
        corrected_value=row.corrected_value,
        doc_type=row.doc_type,
        supplier_key=row.supplier_key,
        context=row.context,
        reviewer_id=row.reviewer_id,
        embedded=row.embedded,
        created_at=row.created_at,
    )


def _rule(row) -> TenantRule:
    return TenantRule(
        id=row.id,
        tenant_id=row.tenant_id,
        doc_type=row.doc_type,
        supplier_key=row.supplier_key,
        field_name=row.field_name,
        rule_type=RuleType(row.rule_type),
        rule_json=row.rule_json or {},
        status=RuleStatus(row.status),
        validation_report=row.validation_report,
        source_correction_ids=row.source_correction_ids or [],
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class PgMemoryRepository:
    def __init__(self, dsn: str) -> None:
        self._engine = create_engine(dsn, future=True)

    def _conn(self, c, tenant_id: str) -> None:
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})

    # --- correction_logs ---
    def add_correction(self, log: CorrectionLog) -> None:
        with self._engine.begin() as c:
            self._conn(c, log.tenant_id)
            c.execute(
                text(
                    "INSERT INTO correction_logs "
                    "(id, tenant_id, document_id, run_id, field_name, original_value, "
                    " corrected_value, doc_type, supplier_key, context, reviewer_id, embedded) "
                    "VALUES (:id,:t,:d,:r,:fn,:ov,:cv,:dt,:sk,:ctx,:rv,:emb) "
                    "ON CONFLICT (id) DO NOTHING"
                ),
                {
                    "id": log.id, "t": log.tenant_id, "d": log.document_id, "r": log.run_id,
                    "fn": log.field_name, "ov": log.original_value, "cv": log.corrected_value,
                    "dt": log.doc_type, "sk": log.supplier_key, "ctx": log.context,
                    "rv": log.reviewer_id, "emb": log.embedded,
                },
            )

    def get_correction(self, tenant_id: str, correction_id: str) -> Optional[CorrectionLog]:
        with self._engine.begin() as c:
            self._conn(c, tenant_id)
            row = c.execute(
                text("SELECT * FROM correction_logs WHERE tenant_id=:t AND id=:i"),
                {"t": tenant_id, "i": correction_id},
            ).first()
        return _correction(row) if row else None

    def list_corrections(
        self, tenant_id: str, *, doc_type: Optional[str] = None, field_name: Optional[str] = None
    ) -> list[CorrectionLog]:
        with self._engine.begin() as c:
            self._conn(c, tenant_id)
            rows = c.execute(
                text(
                    "SELECT * FROM correction_logs WHERE tenant_id=:t "
                    "AND (CAST(:dt AS text) IS NULL OR doc_type=CAST(:dt AS text)) "
                    "AND (CAST(:fn AS text) IS NULL OR field_name=CAST(:fn AS text)) "
                    "ORDER BY created_at"
                ),
                {"t": tenant_id, "dt": doc_type, "fn": field_name},
            ).all()
        return [_correction(r) for r in rows]

    def count_corrections(
        self, tenant_id: str, doc_type: Optional[str], field_name: Optional[str]
    ) -> int:
        with self._engine.begin() as c:
            self._conn(c, tenant_id)
            return c.execute(
                text(
                    "SELECT count(*) FROM correction_logs WHERE tenant_id=:t "
                    "AND (CAST(:dt AS text) IS NULL OR doc_type=CAST(:dt AS text)) "
                    "AND (CAST(:fn AS text) IS NULL OR field_name=CAST(:fn AS text))"
                ),
                {"t": tenant_id, "dt": doc_type, "fn": field_name},
            ).scalar_one()

    # --- tenant_memories ---
    def next_vector_id(self, tenant_id: str) -> int:
        with self._engine.begin() as c:
            self._conn(c, tenant_id)
            return c.execute(
                text(
                    "SELECT coalesce(max(faiss_vector_id), -1) + 1 "
                    "FROM tenant_memories WHERE tenant_id=:t"
                ),
                {"t": tenant_id},
            ).scalar_one()

    def add_memory(self, mem: TenantMemory) -> None:
        with self._engine.begin() as c:
            self._conn(c, mem.tenant_id)
            c.execute(
                text(
                    "INSERT INTO tenant_memories "
                    "(id, tenant_id, correction_log_id, faiss_vector_id, embed_model) "
                    "VALUES (:id,:t,:cl,:vid,:em) ON CONFLICT (id) DO NOTHING"
                ),
                {"id": mem.id, "t": mem.tenant_id, "cl": mem.correction_log_id,
                 "vid": mem.faiss_vector_id, "em": mem.embed_model},
            )
            c.execute(
                text("UPDATE correction_logs SET embedded=true WHERE tenant_id=:t AND id=:i"),
                {"t": mem.tenant_id, "i": mem.correction_log_id},
            )

    def get_memory_by_vector(self, tenant_id: str, vector_id: int) -> Optional[TenantMemory]:
        with self._engine.begin() as c:
            self._conn(c, tenant_id)
            row = c.execute(
                text("SELECT * FROM tenant_memories WHERE tenant_id=:t AND faiss_vector_id=:v"),
                {"t": tenant_id, "v": vector_id},
            ).first()
        return _memory(row) if row else None

    def list_memories(self, tenant_id: str) -> list[TenantMemory]:
        with self._engine.begin() as c:
            self._conn(c, tenant_id)
            rows = c.execute(
                text("SELECT * FROM tenant_memories WHERE tenant_id=:t ORDER BY faiss_vector_id"),
                {"t": tenant_id},
            ).all()
        return [_memory(r) for r in rows]

    # --- tenant_rules ---
    def add_rule(self, rule: TenantRule) -> None:
        self._upsert_rule(rule, insert=True)

    def update_rule(self, rule: TenantRule) -> None:
        self._upsert_rule(rule, insert=False)

    def _upsert_rule(self, rule: TenantRule, *, insert: bool) -> None:
        params = {
            "id": rule.id, "t": rule.tenant_id, "dt": rule.doc_type, "sk": rule.supplier_key,
            "fn": rule.field_name, "rt": rule.rule_type.value,
            "rj": json.dumps(rule.rule_json),
            "st": rule.status.value,
            "vr": json.dumps(rule.validation_report) if rule.validation_report is not None else None,
            "sc": json.dumps(rule.source_correction_ids),
            "cb": rule.created_by,
        }
        with self._engine.begin() as c:
            self._conn(c, rule.tenant_id)
            c.execute(
                text(
                    "INSERT INTO tenant_rules "
                    "(id, tenant_id, doc_type, supplier_key, field_name, rule_type, rule_json, "
                    " status, validation_report, source_correction_ids, created_by) "
                    "VALUES (:id,:t,:dt,:sk,:fn,:rt, CAST(:rj AS jsonb), :st, "
                    " CAST(:vr AS jsonb), CAST(:sc AS jsonb), :cb) "
                    "ON CONFLICT (id) DO UPDATE SET "
                    " doc_type=EXCLUDED.doc_type, supplier_key=EXCLUDED.supplier_key, "
                    " field_name=EXCLUDED.field_name, rule_type=EXCLUDED.rule_type, "
                    " rule_json=EXCLUDED.rule_json, status=EXCLUDED.status, "
                    " validation_report=EXCLUDED.validation_report, "
                    " source_correction_ids=EXCLUDED.source_correction_ids, updated_at=now()"
                ),
                params,
            )

    def get_rule(self, tenant_id: str, rule_id: str) -> Optional[TenantRule]:
        with self._engine.begin() as c:
            self._conn(c, tenant_id)
            row = c.execute(
                text("SELECT * FROM tenant_rules WHERE tenant_id=:t AND id=:i"),
                {"t": tenant_id, "i": rule_id},
            ).first()
        return _rule(row) if row else None

    def list_rules(
        self, tenant_id: str, *, doc_type: Optional[str] = None, status: Optional[RuleStatus] = None
    ) -> list[TenantRule]:
        with self._engine.begin() as c:
            self._conn(c, tenant_id)
            rows = c.execute(
                text(
                    "SELECT * FROM tenant_rules WHERE tenant_id=:t "
                    "AND (CAST(:dt AS text) IS NULL OR doc_type IS NULL OR doc_type=CAST(:dt AS text)) "
                    "AND (CAST(:st AS text) IS NULL OR status=CAST(:st AS text)) ORDER BY created_at"
                ),
                {"t": tenant_id, "dt": doc_type, "st": status.value if status else None},
            ).all()
        return [_rule(r) for r in rows]


def _memory(row) -> TenantMemory:
    return TenantMemory(
        id=row.id,
        tenant_id=row.tenant_id,
        correction_log_id=row.correction_log_id,
        faiss_vector_id=row.faiss_vector_id,
        embed_model=row.embed_model,
        created_at=row.created_at,
    )
