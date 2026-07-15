"""リポジトリ抽象と In-Memory 実装（テスト・dev 用）。

本番は db.PgRepository（PostgreSQL + RLS）を注入する。エンドポイントは本 Protocol
のみに依存する。tenant_id は全メソッドで必須（テナント分離, §11）。
"""

from __future__ import annotations

from typing import Optional, Protocol

from newfan_gateway.records import (
    CorrectionRecord,
    DocumentRecord,
    JobRecord,
    PageRecord,
    RunRecord,
)


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
    def set_document_status(self, tenant_id: str, document_id: str, status: str) -> None: ...

    def create_job(self, job: JobRecord) -> None: ...
    def get_job(self, tenant_id: str, job_id: str) -> Optional[JobRecord]: ...

    def add_corrections(self, corrections: list[CorrectionRecord]) -> None: ...
    def list_corrections(self, tenant_id: str, run_id: str) -> list[CorrectionRecord]: ...
    def list_review_runs(self, tenant_id: str) -> list[RunRecord]: ...


class InMemoryRepository:
    def __init__(self) -> None:
        self._docs: dict[str, DocumentRecord] = {}
        self._pages: dict[str, list[PageRecord]] = {}
        self._runs: dict[str, RunRecord] = {}
        self._jobs: dict[str, JobRecord] = {}
        self._corrections: list[CorrectionRecord] = []

    @staticmethod
    def _owned(rec_tenant: str, tenant_id: str) -> bool:
        return rec_tenant == tenant_id

    def create_document(self, doc: DocumentRecord, pages: list[PageRecord]) -> None:
        self._docs[doc.id] = doc
        self._pages[doc.id] = list(pages)

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

    def set_document_status(self, tenant_id: str, document_id: str, status: str) -> None:
        doc = self.get_document(tenant_id, document_id)
        if doc is not None:
            doc.status = status

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
