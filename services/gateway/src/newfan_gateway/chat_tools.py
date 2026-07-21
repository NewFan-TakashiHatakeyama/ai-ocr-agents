"""チャットグラフのツール実体（§3.3 / §4.5）。

設計書のツール: get_result / explain_field / rerun_extract / update_schema /
search_documents / manage_rules。読み取り系は即実行、書込み系（rerun_extract /
update_schema / manage_rules）は confirm_action を経てから実行する。

グラフから DB/キューへ触るための薄い層。Repository/AdminRepository/Queue は
呼び出し側が注入する（テストは InMemory 実装）。
"""

from __future__ import annotations

from typing import Any, Optional

from newfan_gateway.admin import AdminRepository
from newfan_gateway.ids import new_id
from newfan_gateway.queue import Queue
from newfan_gateway.records import JobRecord, RunRecord, SchemaFieldDef
from newfan_gateway.repository import Repository

# 書込み系。実行前に必ずユーザー確認を挟む（§3.3「書込み系ツールは実行前にユーザー確認ステップを必須」）
WRITE_TOOLS = frozenset({"rerun_extract", "update_schema", "manage_rules"})


class ChatTools:
    def __init__(self, *, repo: Repository, admin: AdminRepository, queue: Queue) -> None:
        self._repo = repo
        self._admin = admin
        self._queue = queue

    # ---------- 読み取り ----------

    def get_result(self, tenant_id: str, document_id: str) -> dict[str, Any]:
        """確定/抽出結果の要約を返す。"""
        run = self._repo.get_latest_run(tenant_id, document_id)
        if run is None:
            return {"error": "この文書の抽出結果がありません"}
        return {
            "document_id": document_id,
            "run_id": run.id,
            "status": run.status,
            "fields": [
                {
                    "name": f.name,
                    "value": f.value_normalized or f.value_raw,
                    "confidence": f.confidence,
                    "review_status": getattr(f.review_status, "value", f.review_status),
                }
                for f in run.fields
            ],
            "table_count": len(run.tables),
        }

    def explain_field(
        self, tenant_id: str, document_id: str, field_name: str
    ) -> dict[str, Any]:
        """項目の grounding（どこから読んだか）を返す（§3.3）。"""
        run = self._repo.get_latest_run(tenant_id, document_id)
        if run is None:
            return {"error": "この文書の抽出結果がありません"}
        for f in run.fields:
            if f.name == field_name:
                return {
                    "name": f.name,
                    "value": f.value_normalized or f.value_raw,
                    "confidence": f.confidence,
                    "grounding_score": f.grounding_score,
                    "page": f.page,
                    "bbox": f.bbox,
                    "source_quote": f.source_quote,
                }
        known = [f.name for f in run.fields]
        return {"error": f"項目 {field_name} は見つかりません", "available": known}

    def search_documents(
        self, tenant_id: str, *, status: Optional[str] = None, limit: int = 20
    ) -> dict[str, Any]:
        rows, _ = self._repo.list_documents(
            tenant_id, status=status, cursor=None, limit=min(limit, 50)
        )
        return {
            "count": len(rows),
            "items": [
                {
                    "document_id": d.id,
                    "status": d.status,
                    "doc_type": d.doc_type,
                    "external_ref": d.external_ref,
                }
                for d in rows
            ],
        }

    def list_rules(self, tenant_id: str, *, status: Optional[str] = None) -> dict[str, Any]:
        rules = self._admin.list_rules(tenant_id, status=status)
        return {
            "count": len(rules),
            "items": [
                {
                    "rule_id": r.id,
                    "status": r.status,
                    "doc_type": r.doc_type,
                    "field_name": r.field_name,
                    "rule_type": r.rule_type,
                }
                for r in rules
            ],
        }

    # ---------- 書込み（confirm_action の後で呼ばれる） ----------

    def rerun_extract(
        self,
        tenant_id: str,
        document_id: str,
        *,
        schema_id: Optional[str] = None,
        options: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """新しい Run を発行して job_id を返す（§4.5）。

        競合は「処理中（processing）」のみ拒否する。needs_review は拒否しない。
        チャットからの再抽出の主な用途が「レビュー中の帳票を、スキーマを直して取り直す」
        （§4.5 の schema_patch 付き再実行）であり、needs_review を弾くと機能が成立しない。
        REST の /extract は needs_review も E1005 で弾くが、あちらは外部連携の二重投入防止で
        目的が違う。
        """
        if self._repo.get_document(tenant_id, document_id) is None:
            return {"ok": False, "message": "ドキュメントが見つかりません"}
        if self._repo.has_processing_run(tenant_id, document_id):
            return {"ok": False, "message": "現在処理中です。完了を待ってください。"}

        run_id, job_id = new_id("run"), new_id("job")
        self._repo.create_run(
            RunRecord(
                id=run_id,
                tenant_id=tenant_id,
                document_id=document_id,
                schema_id=schema_id,
                status="processing",
                options=options or {},
            )
        )
        self._repo.create_job(
            JobRecord(id=job_id, tenant_id=tenant_id, kind="extract", ref_id=run_id)
        )
        self._repo.set_document_status(tenant_id, document_id, "queued")
        self._queue.enqueue(
            "q.extract", {"job_id": job_id, "tenant_id": tenant_id, "run_id": run_id}
        )
        return {"ok": True, "job_id": job_id, "run_id": run_id}

    def update_schema(
        self, tenant_id: str, doc_type: str, field: dict[str, Any]
    ) -> dict[str, Any]:
        cur = self._admin.get_schema(tenant_id, doc_type)
        fields = list(cur.fields) if cur else []
        if any(f.name == field.get("name") for f in fields):
            return {"ok": False, "message": "同名の項目が既に存在します。"}
        fields.append(SchemaFieldDef(**field))
        rec = self._admin.put_schema(tenant_id, doc_type, fields)
        return {"ok": True, "doc_type": rec.doc_type, "version": rec.version}

    def manage_rules(self, tenant_id: str, rule_id: str, status: str) -> dict[str, Any]:
        """ルールの承認/無効化（§5.8）。"""
        if status not in ("active", "rejected", "disabled"):
            return {"ok": False, "message": f"status は active/rejected/disabled: {status}"}
        rule = self._admin.get_rule(tenant_id, rule_id)
        if rule is None:
            return {"ok": False, "message": "ルールが見つかりません"}
        rec = self._admin.set_rule_status(tenant_id, rule_id, status)
        if rec is None:  # 取得と更新の間で消えた等
            return {"ok": False, "message": "ルールの更新に失敗しました"}
        return {"ok": True, "rule_id": rec.id, "status": rec.status}
