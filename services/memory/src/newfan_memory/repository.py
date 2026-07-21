"""メモリ/ルールの正本リポジトリ（§5.8.3: 正本は PostgreSQL）。

エンドポイント/サービスは本 Protocol にのみ依存。テストは InMemory、本番は Pg 実装を注入。
"""

from __future__ import annotations

from typing import Optional, Protocol

from newfan_memory.records import CorrectionLog, RuleStatus, TenantMemory, TenantRule


class MemoryRepository(Protocol):
    def add_correction(self, log: CorrectionLog) -> None: ...
    def get_correction(self, tenant_id: str, correction_id: str) -> Optional[CorrectionLog]: ...
    def list_corrections(
        self, tenant_id: str, *, doc_type: Optional[str] = None, field_name: Optional[str] = None
    ) -> list[CorrectionLog]: ...
    def count_corrections(
        self, tenant_id: str, doc_type: Optional[str], field_name: Optional[str]
    ) -> int: ...

    def next_vector_id(self, tenant_id: str) -> int: ...
    def add_memory(self, mem: TenantMemory) -> None: ...
    def get_memory_by_vector(self, tenant_id: str, vector_id: int) -> Optional[TenantMemory]: ...
    def list_memories(self, tenant_id: str) -> list[TenantMemory]: ...

    def add_rule(self, rule: TenantRule) -> None: ...
    def update_rule(self, rule: TenantRule) -> None: ...
    def get_rule(self, tenant_id: str, rule_id: str) -> Optional[TenantRule]: ...
    def list_rules(
        self,
        tenant_id: str,
        *,
        doc_type: Optional[str] = None,
        status: Optional[RuleStatus] = None,
    ) -> list[TenantRule]: ...


class InMemoryMemoryRepository:
    def __init__(self) -> None:
        self._corrections: dict[str, CorrectionLog] = {}
        self._memories: dict[str, TenantMemory] = {}
        self._rules: dict[str, TenantRule] = {}
        self._vector_seq: dict[str, int] = {}

    def add_correction(self, log: CorrectionLog) -> None:
        self._corrections[log.id] = log

    def get_correction(self, tenant_id: str, correction_id: str) -> Optional[CorrectionLog]:
        log = self._corrections.get(correction_id)
        return log if log and log.tenant_id == tenant_id else None

    def list_corrections(
        self, tenant_id: str, *, doc_type: Optional[str] = None, field_name: Optional[str] = None
    ) -> list[CorrectionLog]:
        return [
            c
            for c in self._corrections.values()
            if c.tenant_id == tenant_id
            and (doc_type is None or c.doc_type == doc_type)
            and (field_name is None or c.field_name == field_name)
        ]

    def count_corrections(
        self, tenant_id: str, doc_type: Optional[str], field_name: Optional[str]
    ) -> int:
        return len(self.list_corrections(tenant_id, doc_type=doc_type, field_name=field_name))

    def next_vector_id(self, tenant_id: str) -> int:
        nxt = self._vector_seq.get(tenant_id, 0)
        self._vector_seq[tenant_id] = nxt + 1
        return nxt

    def add_memory(self, mem: TenantMemory) -> None:
        self._memories[mem.id] = mem
        c = self._corrections.get(mem.correction_log_id)
        if c is not None:
            c.embedded = True

    def get_memory_by_vector(self, tenant_id: str, vector_id: int) -> Optional[TenantMemory]:
        for m in self._memories.values():
            if m.tenant_id == tenant_id and m.faiss_vector_id == vector_id:
                return m
        return None

    def list_memories(self, tenant_id: str) -> list[TenantMemory]:
        return [m for m in self._memories.values() if m.tenant_id == tenant_id]

    def add_rule(self, rule: TenantRule) -> None:
        self._rules[rule.id] = rule

    def update_rule(self, rule: TenantRule) -> None:
        self._rules[rule.id] = rule

    def get_rule(self, tenant_id: str, rule_id: str) -> Optional[TenantRule]:
        r = self._rules.get(rule_id)
        return r if r and r.tenant_id == tenant_id else None

    def list_rules(
        self,
        tenant_id: str,
        *,
        doc_type: Optional[str] = None,
        status: Optional[RuleStatus] = None,
    ) -> list[TenantRule]:
        return [
            r
            for r in self._rules.values()
            if r.tenant_id == tenant_id
            and (doc_type is None or r.doc_type in (None, doc_type))
            and (status is None or r.status == status)
        ]
