"""リポジトリ抽象と In-Memory 実装（テスト・dev 用）。

本番は db.PgRepository（PostgreSQL + RLS）を注入する。エンドポイントは本 Protocol
のみに依存する。tenant_id は全メソッドで必須（テナント分離, §11）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional, Protocol

from newfan_gateway.records import (
    CorrectionRecord,
    DocumentRecord,
    JobRecord,
    PageRecord,
    RunRecord,
)


class DocumentGoneError(Exception):
    """削除トランザクションの最中に、対象の帳票が他から消されていた。

    同時に 2 回 DELETE したときの敗者側で起きる。汎用例外にすると router が
    500「内部エラー」を返してしまい、利用者には何が起きたか伝わらない。
    「不在」として 400/E1001 に翻訳できるよう専用の型にする。
    """


class Repository(Protocol):
    def create_document(self, doc: DocumentRecord, pages: list[PageRecord]) -> None: ...
    def get_document(self, tenant_id: str, document_id: str) -> Optional[DocumentRecord]: ...
    def list_documents(
        self, tenant_id: str, *, status: Optional[str], cursor: Optional[str], limit: int
    ) -> tuple[list[DocumentRecord], Optional[str]]: ...
    def get_pages(self, tenant_id: str, document_id: str) -> list[PageRecord]: ...

    def has_active_run(self, tenant_id: str, document_id: str) -> bool: ...
    def has_processing_run(self, tenant_id: str, document_id: str) -> bool: ...
    def create_run(self, run: RunRecord) -> None: ...
    def get_run(self, tenant_id: str, run_id: str) -> Optional[RunRecord]: ...
    def get_latest_run(self, tenant_id: str, document_id: str) -> Optional[RunRecord]: ...
    def supersede_review_runs(self, tenant_id: str, document_id: str) -> int: ...
    def set_document_status(self, tenant_id: str, document_id: str, status: str) -> None: ...

    def get_delete_blocker(
        self, tenant_id: str, document_id: str, *, stale_minutes: int
    ) -> Optional[str]:
        """削除を止める理由（"document_busy" / "processing"）。消せるなら None。

        stale_minutes より古い processing は「停止したまま固着した run」とみなして
        通す（mark_run_failed はワーカーの例外ハンドラ経由でしか呼ばれないため、
        タスク入れ替えや OOM では processing が永久に残る）。
        """
        ...

    def delete_document(
        self, tenant_id: str, document_id: str, *, actor_id: str, detail: dict[str, object]
    ) -> Optional[dict[str, int]]:
        """帳票と派生データを消し、テーブルごとの削除件数を返す（不在なら None）。

        audit_logs へ削除記録を同一トランザクションで書く。監査が別トランザクション
        だと「消えたのに痕跡が無い」が成立し得る。
        """
        ...

    def create_job(self, job: JobRecord) -> None: ...
    def get_job(self, tenant_id: str, job_id: str) -> Optional[JobRecord]: ...

    def add_corrections(self, corrections: list[CorrectionRecord]) -> None: ...
    def list_corrections(self, tenant_id: str, run_id: str) -> list[CorrectionRecord]: ...
    def list_review_runs(self, tenant_id: str) -> list[RunRecord]: ...
    def list_hitl_boosts(self, tenant_id: str) -> dict[str, int]:
        """document_id → hitl_gate の priority_boost（waiting_hitl の workflow_runs 由来, §16 P5）。"""
        ...


class InMemoryRepository:
    def __init__(self) -> None:
        self._docs: dict[str, DocumentRecord] = {}
        self._pages: dict[str, list[PageRecord]] = {}
        self._runs: dict[str, RunRecord] = {}
        self._jobs: dict[str, JobRecord] = {}
        self._corrections: list[CorrectionRecord] = []
        self._hitl_boosts: dict[tuple[str, str], int] = {}
        # documents.updated_at 相当（DocumentRecord は列を持たない）。削除の
        # 「確定処理中の窓」判定を Pg と同じ観測結果にするために持つ。
        self._doc_touched: dict[str, datetime] = {}
        self.audits: list[dict[str, object]] = []

    @staticmethod
    def _owned(rec_tenant: str, tenant_id: str) -> bool:
        return rec_tenant == tenant_id

    def create_document(self, doc: DocumentRecord, pages: list[PageRecord]) -> None:
        self._docs[doc.id] = doc
        self._pages[doc.id] = list(pages)
        self._doc_touched[doc.id] = doc.created_at

    def get_document(self, tenant_id: str, document_id: str) -> Optional[DocumentRecord]:
        doc = self._docs.get(document_id)
        return doc if doc and self._owned(doc.tenant_id, tenant_id) else None

    def list_documents(
        self, tenant_id: str, *, status: Optional[str], cursor: Optional[str], limit: int
    ) -> tuple[list[DocumentRecord], Optional[str]]:
        rows = [
            d
            for d in self._docs.values()
            if d.tenant_id == tenant_id and (status is None or d.status == status)
        ]
        rows.sort(key=lambda d: d.created_at, reverse=True)
        start = 0
        if cursor is not None:
            ids = [d.id for d in rows]
            start = ids.index(cursor) + 1 if cursor in ids else 0
        page = rows[start : start + limit]
        next_cursor = page[-1].id if len(rows) > start + limit else None
        return page, next_cursor

    def get_pages(self, tenant_id: str, document_id: str) -> list[PageRecord]:
        if self.get_document(tenant_id, document_id) is None:
            return []
        return self._pages.get(document_id, [])

    def has_active_run(self, tenant_id: str, document_id: str) -> bool:
        return any(
            r.document_id == document_id
            and r.tenant_id == tenant_id
            and r.status in ("processing", "needs_review")
            for r in self._runs.values()
        )

    def has_processing_run(self, tenant_id: str, document_id: str) -> bool:
        """実行中（processing）の Run があるか。

        has_active_run は needs_review も含むが、こちらは「今まさに処理中」だけを見る。
        チャットからの再抽出（§4.5）は needs_review の帳票を取り直す用途が主で、
        needs_review を弾くと成立しないため区別が要る。
        """
        return any(
            r.document_id == document_id
            and self._owned(r.tenant_id, tenant_id)
            and r.status == "processing"
            for r in self._runs.values()
        )

    def create_run(self, run: RunRecord) -> None:
        self._runs[run.id] = run

    def get_run(self, tenant_id: str, run_id: str) -> Optional[RunRecord]:
        run = self._runs.get(run_id)
        return run if run and self._owned(run.tenant_id, tenant_id) else None

    def get_latest_run(self, tenant_id: str, document_id: str) -> Optional[RunRecord]:
        runs = [
            r
            for r in self._runs.values()
            if r.document_id == document_id and r.tenant_id == tenant_id
        ]
        runs.sort(key=lambda r: r.started_at, reverse=True)
        return runs[0] if runs else None

    def supersede_review_runs(self, tenant_id: str, document_id: str) -> int:
        """needs_review の run を superseded へ落とす（設計 §3.1 再抽出ボタン）。

        レビュー待ちのまま再抽出すると run が 2 本並び、get_latest_run・削除ブロッカー・
        ワークフローの hitl_gate が古い方を見続ける。新しい run を作る**前**に
        旧 run を終端させる。processing は触らない（実行中の worker がいる）。
        """
        n = 0
        for r in self._runs.values():
            if (
                r.document_id == document_id
                and self._owned(r.tenant_id, tenant_id)
                and r.status == "needs_review"
            ):
                r.status = "superseded"
                n += 1
        return n

    def set_document_status(self, tenant_id: str, document_id: str, status: str) -> None:
        doc = self.get_document(tenant_id, document_id)
        if doc is not None:
            doc.status = status
            self._doc_touched[document_id] = datetime.now(timezone.utc)

    # --- 削除（§6.2 DELETE /documents/{id}） ---

    # 「今この帳票に触っている処理がある」とみなす documents.status。
    # confirm は documents だけ in_review にして extraction_runs は needs_review の
    # ままにするため、run の status だけ見ると確定処理中の窓を素通りする。
    _BUSY_DOC_STATUS = ("queued", "processing", "in_review")

    def get_delete_blocker(
        self, tenant_id: str, document_id: str, *, stale_minutes: int
    ) -> Optional[str]:
        doc = self.get_document(tenant_id, document_id)
        if doc is None:
            return None
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=stale_minutes)
        touched = self._doc_touched.get(document_id, doc.created_at)
        if doc.status in self._BUSY_DOC_STATUS and touched > cutoff:
            return "document_busy"
        for r in self._runs.values():
            if (
                r.document_id == document_id
                and self._owned(r.tenant_id, tenant_id)
                and r.status == "processing"
                and r.started_at > cutoff
            ):
                return "processing"
        return None

    def delete_document(
        self, tenant_id: str, document_id: str, *, actor_id: str, detail: dict[str, object]
    ) -> Optional[dict[str, int]]:
        doc = self.get_document(tenant_id, document_id)
        if doc is None:
            return None
        run_ids = {
            r.id
            for r in self._runs.values()
            if r.document_id == document_id and self._owned(r.tenant_id, tenant_id)
        }
        corrections = [c for c in self._corrections if c.document_id == document_id]
        jobs = [j for j in self._jobs.values() if j.ref_id in run_ids]
        counts = {
            "runs_deleted": len(run_ids),
            "corrections_deleted": len(corrections),
            "jobs_deleted": len(jobs),
            "pages_deleted": len(self._pages.get(document_id, [])),
        }
        self._corrections = [c for c in self._corrections if c.document_id != document_id]
        for j in jobs:
            del self._jobs[j.id]
        for rid in run_ids:
            del self._runs[rid]
        self._pages.pop(document_id, None)
        self._doc_touched.pop(document_id, None)
        self._hitl_boosts.pop((tenant_id, document_id), None)
        del self._docs[document_id]
        self.audits.append(
            {
                "tenant_id": tenant_id,
                "actor_id": actor_id,
                "action": "document.delete",
                "target_type": "document",
                "target_id": document_id,
                "detail": {**detail, **counts},
            }
        )
        return counts

    def create_job(self, job: JobRecord) -> None:
        self._jobs[job.id] = job

    def get_job(self, tenant_id: str, job_id: str) -> Optional[JobRecord]:
        job = self._jobs.get(job_id)
        return job if job and self._owned(job.tenant_id, tenant_id) else None

    def add_corrections(self, corrections: list[CorrectionRecord]) -> None:
        self._corrections.extend(corrections)

    def list_corrections(self, tenant_id: str, run_id: str) -> list[CorrectionRecord]:
        return [
            c for c in self._corrections
            if c.run_id == run_id and self._owned(c.tenant_id, tenant_id)
        ]

    def list_review_runs(self, tenant_id: str) -> list[RunRecord]:
        return [
            r
            for r in self._runs.values()
            if r.tenant_id == tenant_id and r.status == "needs_review"
        ]

    def seed_hitl_boost(self, tenant_id: str, document_id: str, boost: int) -> None:
        self._hitl_boosts[(tenant_id, document_id)] = boost

    def list_hitl_boosts(self, tenant_id: str) -> dict[str, int]:
        return {d: b for (t, d), b in self._hitl_boosts.items() if t == tenant_id}
