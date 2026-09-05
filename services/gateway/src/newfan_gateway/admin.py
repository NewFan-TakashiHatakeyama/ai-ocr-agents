"""管理画面（SCR-04/05/06）の Repository 抽象と In-Memory 実装。

スキーマ版管理（§5.5）、ルールライフサイクル（§5.8.4）、KPI 集計（§12.1）。
本番は db.PgAdminRepository を注入。エンドポイントは本 Protocol のみに依存する。
"""

from __future__ import annotations

from typing import Any, Optional, Protocol

from newfan_schemas import RegionRect

from newfan_gateway.ids import new_id
from newfan_gateway.records import (
    ConnectionRecord,
    MemoryRecord,
    MetricsSummary,
    RuleRecord,
    SchemaFieldDef,
    SchemaRecord,
)

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
    def get_schema_by_id(self, tenant_id: str, schema_id: str) -> Optional[SchemaRecord]: ...
    def put_schema(
        self,
        tenant_id: str,
        doc_type: str,
        fields: list[SchemaFieldDef],
        *,
        exclude_regions: Optional[list[RegionRect]] = None,
        source_page_count: Optional[int] = None,
    ) -> SchemaRecord:
        """新版を INSERT する（常に新 version）。

        exclude_regions / source_page_count は **None = 直前版から引き継ぎ**、
        exclude_regions の明示 `[]` のみクリア（設計 §4.4）。旧編集画面と chat 経路は
        これらを送らないため、「省略時 []」にすると旧経路の保存 1 回で除外設定が
        全滅する。引き継ぎを呼び出し側でなく実装側に置くことで、旧経路はコード
        無変更のまま安全になる。戻り値には**引き継ぎ後の実値**を載せる（PUT 応答が
        直後の GET と一致しないと、旧画面が空配列で state を上書きして次の保存で
        本物のクリアを送ってしまう）。
        """
        ...

    # ルール（§5.8.4）
    def create_rule(self, rec: RuleRecord) -> RuleRecord: ...
    def list_rules(
        self, tenant_id: str, *, status: Optional[str] = None, doc_type: Optional[str] = None
    ) -> list[RuleRecord]: ...
    def get_rule(self, tenant_id: str, rule_id: str) -> Optional[RuleRecord]: ...
    def set_rule_status(
        self, tenant_id: str, rule_id: str, status: str
    ) -> Optional[RuleRecord]: ...

    # 修正メモリ（§5.8 / DD-06）
    def list_memories(
        self,
        tenant_id: str,
        *,
        doc_type: Optional[str] = None,
        field_name: Optional[str] = None,
        limit: int = 50,
    ) -> list[MemoryRecord]: ...

    # Webhook 配信先（§6.4）。connections(type='webhook') 行として持つ。
    # secret_ref があれば署名鍵は Secrets Manager 参照（P6。secret は保存しない）
    def add_webhook_endpoint(
        self,
        tenant_id: str,
        *,
        url: str,
        secret: Optional[str],
        name: str,
        secret_ref: Optional[str] = None,
    ) -> ConnectionRecord: ...
    def list_webhook_endpoints(self, tenant_id: str) -> list[ConnectionRecord]: ...

    # 接続管理（§16.5 / P6）
    def create_connection(
        self,
        tenant_id: str,
        *,
        type: str,
        name: str,
        config: dict[str, Any],
        secret_ref: Optional[str],
        allowed_tables: list[str],
    ) -> ConnectionRecord: ...
    def list_connections(self, tenant_id: str) -> list[ConnectionRecord]: ...
    def get_connection(self, tenant_id: str, connection_id: str) -> Optional[ConnectionRecord]: ...
    def set_connection_status(
        self, tenant_id: str, connection_id: str, status: str
    ) -> Optional[ConnectionRecord]: ...

    # KPI（§12.1）
    def metrics_summary(self, tenant_id: str) -> MetricsSummary: ...


class InMemoryAdminRepository:
    def __init__(self) -> None:
        self._schemas: dict[str, SchemaRecord] = {}
        self._rules: dict[str, RuleRecord] = {}
        self._memories: list[MemoryRecord] = []
        self._connections: list[ConnectionRecord] = []
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

    def get_schema_by_id(self, tenant_id: str, schema_id: str) -> Optional[SchemaRecord]:
        rec = self._schemas.get(schema_id)
        return rec if rec and rec.tenant_id == tenant_id else None

    def get_schema(self, tenant_id: str, doc_type: str) -> Optional[SchemaRecord]:
        rows = [s for s in self._schemas.values() if s.tenant_id == tenant_id and s.doc_type == doc_type]
        return max(rows, key=lambda s: s.version) if rows else None

    def put_schema(
        self,
        tenant_id: str,
        doc_type: str,
        fields: list[SchemaFieldDef],
        *,
        exclude_regions: Optional[list[RegionRect]] = None,
        source_page_count: Optional[int] = None,
    ) -> SchemaRecord:
        prev = self.get_schema(tenant_id, doc_type)
        # None = 引き継ぎ（§4.4）。Pg 実装と意味論を揃える。
        regions = (
            list(exclude_regions)
            if exclude_regions is not None
            else (list(prev.exclude_regions) if prev else [])
        )
        pages = (
            source_page_count
            if source_page_count is not None
            else (prev.source_page_count if prev else None)
        )
        rec = SchemaRecord(
            id=new_id("schema"),
            tenant_id=tenant_id,
            doc_type=doc_type,
            version=(prev.version + 1) if prev else 1,
            fields=fields,
            exclude_regions=regions,
            source_page_count=pages,
        )
        self._schemas[rec.id] = rec
        return rec

    # --- rules ---
    def seed_rule(self, rec: RuleRecord) -> None:
        self._rules[rec.id] = rec

    def create_rule(self, rec: RuleRecord) -> RuleRecord:
        self._rules[rec.id] = rec
        return rec

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

    # --- 修正メモリ（§5.8 / DD-06） ---
    def seed_memory(self, rec: MemoryRecord) -> None:
        self._memories.append(rec)

    def list_memories(
        self,
        tenant_id: str,
        *,
        doc_type: Optional[str] = None,
        field_name: Optional[str] = None,
        limit: int = 50,
    ) -> list[MemoryRecord]:
        rows = [
            m
            for m in self._memories
            if m.tenant_id == tenant_id
            and (doc_type is None or m.doc_type == doc_type)
            and (field_name is None or m.field_name == field_name)
        ]
        # 新しい順。学習は追記なので、直近何を覚えたかが最も知りたい情報。
        rows.sort(key=lambda m: m.created_at or "", reverse=True)
        return rows[:limit]

    # --- webhook 配信先（§6.4） ---
    def add_webhook_endpoint(
        self,
        tenant_id: str,
        *,
        url: str,
        secret: Optional[str],
        name: str,
        secret_ref: Optional[str] = None,
    ) -> ConnectionRecord:
        # secret_ref があれば config に秘密を入れない（§16.5）。無ければ旧方式（平文）
        config = {"url": url} if secret_ref else {"url": url, "secret": secret}
        rec = ConnectionRecord(
            id=new_id("connection"),
            tenant_id=tenant_id,
            type="webhook",
            name=name,
            config=config,
            secret_ref=secret_ref,
            # 疎通確認前は untested。export の list_webhook_endpoints は
            # active/tested しか拾わないため、登録しただけでは配信されない（§16.5 の安全策）。
            status="untested",
        )
        self._connections.append(rec)
        return rec

    # --- 接続管理（§16.5 / P6） ---
    def create_connection(
        self,
        tenant_id: str,
        *,
        type: str,
        name: str,
        config: dict[str, Any],
        secret_ref: Optional[str],
        allowed_tables: list[str],
    ) -> ConnectionRecord:
        rec = ConnectionRecord(
            id=new_id("connection"),
            tenant_id=tenant_id,
            type=type,
            name=name,
            config=dict(config),
            secret_ref=secret_ref,
            allowed_tables=list(allowed_tables),
            status="untested",
        )
        self._connections.append(rec)
        return rec

    def list_connections(self, tenant_id: str) -> list[ConnectionRecord]:
        return [c for c in self._connections if c.tenant_id == tenant_id]

    def get_connection(self, tenant_id: str, connection_id: str) -> Optional[ConnectionRecord]:
        for c in self._connections:
            if c.tenant_id == tenant_id and c.id == connection_id:
                return c
        return None

    def set_connection_status(
        self, tenant_id: str, connection_id: str, status: str
    ) -> Optional[ConnectionRecord]:
        rec = self.get_connection(tenant_id, connection_id)
        if rec is None:
            return None
        updated = rec.model_copy(update={"status": status})
        self._connections[self._connections.index(rec)] = updated
        return updated

    def list_webhook_endpoints(self, tenant_id: str) -> list[ConnectionRecord]:
        return [c for c in self._connections if c.tenant_id == tenant_id and c.type == "webhook"]

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
