"""管理画面（SCR-04/05/06）の Repository 抽象と In-Memory 実装。

スキーマ版管理（§5.5）、ルールライフサイクル（§5.8.4）、KPI 集計（§12.1）。
本番は db.PgAdminRepository を注入。エンドポイントは本 Protocol のみに依存する。
"""

from __future__ import annotations

from typing import Any, Optional, Protocol

from newfan_gateway.ids import new_id
from newfan_gateway.records import MetricsSummary, RuleRecord, SchemaFieldDef, SchemaRecord

# ルール有効化の閾値（§2.5 rules.validation_pass: 再現率≥90% かつ 回帰0件）
MIN_REPRODUCTION = 0.9


def is_activatable(report: Optional[dict[str, Any]]) -> bool:
    if not report:
        return False
    repro = float(report.get("reproduction_rate", 0.0) or 0.0)
    regressions = int(report.get("regressions", 1) or 0)
    return repro >= MIN_REPRODUCTION and regressions == 0


class AdminRepository(Protocol):
    # スキーマ（§5.5）
    def list_schemas(self, tenant_id: str) -> list[SchemaRecord]: ...
    def get_schema(self, tenant_id: str, doc_type: str) -> Optional[SchemaRecord]: ...
    def put_schema(
        self, tenant_id: str, doc_type: str, fields: list[SchemaFieldDef]
    ) -> SchemaRecord: ...

    # ルール（§5.8.4）
    def list_rules(
        self, tenant_id: str, *, status: Optional[str] = None, doc_type: Optional[str] = None
    ) -> list[RuleRecord]: ...
    def get_rule(self, tenant_id: str, rule_id: str) -> Optional[RuleRecord]: ...
    def set_rule_status(
        self, tenant_id: str, rule_id: str, status: str
    ) -> Optional[RuleRecord]: ...

    # KPI（§12.1）
    def metrics_summary(self, tenant_id: str) -> MetricsSummary: ...


class InMemoryAdminRepository:
    def __init__(self) -> None:
        self._schemas: dict[str, SchemaRecord] = {}
        self._rules: dict[str, RuleRecord] = {}
        self._metrics: dict[str, MetricsSummary] = {}

    # --- schemas ---
    def seed_schema(self, rec: SchemaRecord) -> None:
        self._schemas[rec.id] = rec

    def list_schemas(self, tenant_id: str) -> list[SchemaRecord]:
        latest: dict[str, SchemaRecord] = {}
        for s in self._schemas.values():
            if s.tenant_id != tenant_id:
                continue
            cur = latest.get(s.doc_type)
            if cur is None or s.version > cur.version:
                latest[s.doc_type] = s
        return sorted(latest.values(), key=lambda s: s.doc_type)

    def get_schema(self, tenant_id: str, doc_type: str) -> Optional[SchemaRecord]:
        rows = [s for s in self._schemas.values() if s.tenant_id == tenant_id and s.doc_type == doc_type]
        return max(rows, key=lambda s: s.version) if rows else None

    def put_schema(
        self, tenant_id: str, doc_type: str, fields: list[SchemaFieldDef]
    ) -> SchemaRecord:
        prev = self.get_schema(tenant_id, doc_type)
        rec = SchemaRecord(
            id=new_id("schema"),
            tenant_id=tenant_id,
            doc_type=doc_type,
            version=(prev.version + 1) if prev else 1,
            fields=fields,
        )
        self._schemas[rec.id] = rec
        return rec

    # --- rules ---
    def seed_rule(self, rec: RuleRecord) -> None:
        self._rules[rec.id] = rec

    def list_rules(
        self, tenant_id: str, *, status: Optional[str] = None, doc_type: Optional[str] = None
    ) -> list[RuleRecord]:
        return [
            r
            for r in self._rules.values()
            if r.tenant_id == tenant_id
            and (status is None or r.status == status)
            and (doc_type is None or r.doc_type in (None, doc_type))
        ]

    def get_rule(self, tenant_id: str, rule_id: str) -> Optional[RuleRecord]:
        r = self._rules.get(rule_id)
        return r if r and r.tenant_id == tenant_id else None

    def set_rule_status(self, tenant_id: str, rule_id: str, status: str) -> Optional[RuleRecord]:
        r = self.get_rule(tenant_id, rule_id)
        if r is None:
            return None
        r.status = status
        return r

    # --- metrics ---
    def set_metrics(self, tenant_id: str, m: MetricsSummary) -> None:
        self._metrics[tenant_id] = m

    def metrics_summary(self, tenant_id: str) -> MetricsSummary:
        if tenant_id in self._metrics:
            return self._metrics[tenant_id]
        active = sum(1 for r in self._rules.values() if r.tenant_id == tenant_id and r.status == "active")
        pending = sum(
            1
            for r in self._rules.values()
            if r.tenant_id == tenant_id and r.status in ("draft", "validating")
        )
        return MetricsSummary(active_rules=active, pending_rules=pending)
