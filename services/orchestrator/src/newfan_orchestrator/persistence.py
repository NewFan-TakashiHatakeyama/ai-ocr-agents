"""抽出グラフの永続化ポート（§4.3 load_context / finalize, §7）。

load_context は DB から document/pages/schema/テナント設定をロードし、finalize は確定データを
保存して status 遷移する。エンドポイント同様に Protocol のみに依存し、テストは InMemory、
本番は db 実装（PgContextStore）を注入する。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

from newfan_schemas import ExtractedField, ReviewItem, TableResult


@dataclass
class LoadedContext:
    document_id: str
    schema: dict[str, Any]
    pages: list[dict[str, Any]]
    tenant_settings: dict[str, Any] = field(default_factory=dict)


class ContextStore(Protocol):
    def load_context(self, tenant_id: str, run_id: str) -> Optional[LoadedContext]:
        """run_id から document/pages/schema/テナント設定をロード（§4.3 load_context）。"""
        ...

    def save_result(
        self,
        tenant_id: str,
        run_id: str,
        *,
        fields: list[ExtractedField],
        tables: list[TableResult],
        review_items: list[ReviewItem],
        status: str,
        fallback_pages: Optional[list[int]] = None,
    ) -> None:
        """extraction_fields/tables を保存し、run/document の status を遷移する（§4.3 finalize）。

        fallback_pages（VL フォールバックしたページ, §5.4/DD-09）は run の metrics に記録し、
        UI がバッジ/バナーで露出できるようにする。
        """
        ...

    def add_run_metrics(self, tenant_id: str, run_id: str, patch: dict[str, float]) -> None:
        """extraction_runs.metrics へ数値を**加算**で追記する（LLM トークン等）。"""
        ...

    def set_job_status(
        self, tenant_id: str, job_id: str, status: str, *, error_code: Optional[str] = None
    ) -> None:
        """jobs.status を遷移する（§9）。status は queued/running/succeeded/failed/dead。

        §6.3 の API は「GET /jobs/{id} を polling して終了を待つ」契約なので、ここが
        更新されないとクライアントは永久に queued を見続ける（実際にそうなっていた）。
        """
        ...


@dataclass
class _RunSeed:
    tenant_id: str
    document_id: str
    schema: dict[str, Any]
    pages: list[dict[str, Any]]
    tenant_settings: dict[str, Any] = field(default_factory=dict)


class InMemoryContextStore:
    """テスト/dev 用。seed_run でコンテキストを投入し、save_result の結果を検証できる。"""

    def __init__(self) -> None:
        self._runs: dict[str, _RunSeed] = {}
        self._document_status: dict[str, str] = {}
        self._run_status: dict[str, str] = {}
        self._saved_fields: dict[str, list[ExtractedField]] = {}
        self._saved_tables: dict[str, list[TableResult]] = {}
        self._saved_fallback: dict[str, list[int]] = {}
        self._result_version: dict[str, int] = {}
        self.run_metrics: dict[str, dict[str, float]] = {}
        self._job_status: dict[str, tuple[str, Optional[str]]] = {}

    def seed_run(
        self,
        run_id: str,
        *,
        tenant_id: str,
        document_id: str,
        schema: dict[str, Any],
        pages: list[dict[str, Any]],
        tenant_settings: Optional[dict[str, Any]] = None,
    ) -> None:
        self._runs[run_id] = _RunSeed(
            tenant_id=tenant_id,
            document_id=document_id,
            schema=schema,
            pages=pages,
            tenant_settings=tenant_settings or {},
        )
        self._document_status[document_id] = "processing"
        self._run_status[run_id] = "processing"
        self._result_version.setdefault(run_id, 1)

    def load_context(self, tenant_id: str, run_id: str) -> Optional[LoadedContext]:
        seed = self._runs.get(run_id)
        if seed is None or seed.tenant_id != tenant_id:
            return None
        return LoadedContext(
            document_id=seed.document_id,
            schema=seed.schema,
            pages=seed.pages,
            tenant_settings=seed.tenant_settings,
        )

    def save_result(
        self,
        tenant_id: str,
        run_id: str,
        *,
        fields: list[ExtractedField],
        tables: list[TableResult],
        review_items: list[ReviewItem],
        status: str,
        fallback_pages: Optional[list[int]] = None,
    ) -> None:
        seed = self._runs.get(run_id)
        if seed is None or seed.tenant_id != tenant_id:
            raise KeyError(f"unknown run {run_id}")
        self._saved_fields[run_id] = list(fields)
        self._saved_tables[run_id] = list(tables)
        self._saved_fallback[run_id] = list(fallback_pages or [])
        self._run_status[run_id] = status
        self._document_status[seed.document_id] = status
        self._result_version[run_id] = self._result_version.get(run_id, 1)

    def add_run_metrics(self, tenant_id: str, run_id: str, patch: dict[str, float]) -> None:
        m = self.run_metrics.setdefault(run_id, {})
        for k, v in patch.items():
            m[k] = m.get(k, 0) + v

    def set_job_status(
        self, tenant_id: str, job_id: str, status: str, *, error_code: Optional[str] = None
    ) -> None:
        self._job_status[job_id] = (status, error_code)

    # --- テスト用アクセサ ---
    def job_status(self, job_id: str) -> Optional[tuple[str, Optional[str]]]:
        return self._job_status.get(job_id)

    def run_status(self, run_id: str) -> Optional[str]:
        return self._run_status.get(run_id)

    def document_status(self, document_id: str) -> Optional[str]:
        return self._document_status.get(document_id)

    def saved_fields(self, run_id: str) -> list[ExtractedField]:
        return self._saved_fields.get(run_id, [])

    def saved_fallback_pages(self, run_id: str) -> list[int]:
        return self._saved_fallback.get(run_id, [])
