"""ワークフロー定義の Repository（§16 設計 v0.2 §11 / P2）。

Protocol + InMemory 実装。本番の PostgreSQL 実装は db.PgWorkflowsRepository
（sqlalchemy は runtime extra のため、このモジュールには import しない）。

版管理の契約:
- create は version=1 / status=draft
- update は **常に新版**（version+1）とし、status を draft に戻す。
  active のまま定義が差し替わると「有効化＝版の固定」（§11.1）が破れるため、
  変更後は必ず lint → activate をやり直させる。
"""

from __future__ import annotations

from typing import Any, Optional, Protocol

from newfan_gateway.ids import new_id
from newfan_gateway.records import WorkflowRecord

# activate を許すノード種別（実装済み Phase のもののみ）。
# 保存と lint は 13 種すべて通すが、実行できない種別を含むワークフローの有効化は
# ここで止める。「エディタに置けるのに動かない」を有効化の境界で明示的に断る。
# Phase が進むたびにこの集合を広げ、テストで固定する（P3: manual 実行の最小経路）。
IMPLEMENTED_NODE_TYPES: frozenset[str] = frozenset(
    {
        "source.manual",
        "process.extract",
        "branch.condition",
        "transform.map_fields",
        "sink.webhook",
    }
)


class WorkflowsRepository(Protocol):
    def list_workflows(self, tenant_id: str) -> list[WorkflowRecord]: ...
    def get_workflow(self, tenant_id: str, workflow_id: str) -> Optional[WorkflowRecord]: ...
    def create_workflow(self, rec: WorkflowRecord) -> WorkflowRecord: ...
    def update_workflow(
        self,
        tenant_id: str,
        workflow_id: str,
        *,
        graph_json: dict[str, Any],
        name: Optional[str] = None,
        auto_confirm: Optional[bool] = None,
    ) -> Optional[WorkflowRecord]: ...
    def set_status(
        self, tenant_id: str, workflow_id: str, status: str
    ) -> Optional[WorkflowRecord]: ...

    # activate 時の lint L009/L010 の参照解決
    def schema_exists(self, tenant_id: str, schema_id: str) -> bool: ...
    def connection_ok(self, tenant_id: str, connection_id: str) -> bool: ...

    # §16.8: config 変更・有効化/停止は audit_logs へ（actor=human/agent）
    def record_audit(
        self,
        tenant_id: str,
        *,
        actor_id: str,
        action: str,
        target_id: str,
        detail: dict[str, Any],
    ) -> None: ...


class InMemoryWorkflowsRepository:
    def __init__(self) -> None:
        self._rows: dict[str, WorkflowRecord] = {}
        self._schemas: set[tuple[str, str]] = set()
        self._connections: set[tuple[str, str]] = set()
        self.audits: list[dict[str, Any]] = []

    # --- テスト/dev 用 seed ---
    def seed_schema_id(self, tenant_id: str, schema_id: str) -> None:
        self._schemas.add((tenant_id, schema_id))

    def seed_connection(self, tenant_id: str, connection_id: str) -> None:
        self._connections.add((tenant_id, connection_id))

    # --- Repository ---
    def list_workflows(self, tenant_id: str) -> list[WorkflowRecord]:
        rows = [w for w in self._rows.values() if w.tenant_id == tenant_id]
        return sorted(rows, key=lambda w: w.updated_at or "", reverse=True)

    def get_workflow(self, tenant_id: str, workflow_id: str) -> Optional[WorkflowRecord]:
        w = self._rows.get(workflow_id)
        return w if w and w.tenant_id == tenant_id else None

    def create_workflow(self, rec: WorkflowRecord) -> WorkflowRecord:
        self._rows[rec.id] = rec
        return rec

    def update_workflow(
        self,
        tenant_id: str,
        workflow_id: str,
        *,
        graph_json: dict[str, Any],
        name: Optional[str] = None,
        auto_confirm: Optional[bool] = None,
    ) -> Optional[WorkflowRecord]:
        w = self.get_workflow(tenant_id, workflow_id)
        if w is None:
            return None
        w.graph_json = graph_json
        if name is not None:
            w.name = name
        if auto_confirm is not None:
            w.auto_confirm = auto_confirm
        w.version += 1
        w.status = "draft"  # 新版は必ず lint → activate をやり直す（§11.1）
        return w

    def set_status(
        self, tenant_id: str, workflow_id: str, status: str
    ) -> Optional[WorkflowRecord]:
        w = self.get_workflow(tenant_id, workflow_id)
        if w is None:
            return None
        w.status = status
        return w

    def schema_exists(self, tenant_id: str, schema_id: str) -> bool:
        return (tenant_id, schema_id) in self._schemas

    def connection_ok(self, tenant_id: str, connection_id: str) -> bool:
        return (tenant_id, connection_id) in self._connections

    def record_audit(
        self,
        tenant_id: str,
        *,
        actor_id: str,
        action: str,
        target_id: str,
        detail: dict[str, Any],
    ) -> None:
        self.audits.append(
            {
                "id": new_id("audit"),
                "tenant_id": tenant_id,
                "actor_id": actor_id,
                "action": action,
                "target_id": target_id,
                "detail": detail,
            }
        )
