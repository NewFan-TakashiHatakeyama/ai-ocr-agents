"""REST エンドポイント（§6.2 / §6.3）。"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Form, Header, Request, UploadFile

from newfan_gateway import dto
from newfan_gateway.auth import Principal
from newfan_gateway.config import Settings
from newfan_gateway.deps import (
    get_ingestor,
    get_orchestrator,
    get_queue,
    get_repo,
    get_settings,
    require_role,
)
from newfan_gateway.errors import ApiError
from newfan_gateway.ids import new_id
from newfan_gateway.ports import Ingestor, OrchestratorClient
from newfan_gateway.queue import Queue
from newfan_gateway.records import (
    CorrectionRecord,
    DocumentRecord,
    JobRecord,
    PageRecord,
    RunRecord,
)
from newfan_gateway.repository import Repository
from newfan_ingest import IngestError, UploadInput

router = APIRouter(prefix="/v1")


def _idempotency_hit(request: Request, key: Optional[str], tenant_id: str) -> Optional[Any]:
    if not key:
        return None
    return request.app.state.idempotency.get((tenant_id, key))


def _idempotency_store(request: Request, key: Optional[str], tenant_id: str, value: Any) -> None:
    if key:
        request.app.state.idempotency[(tenant_id, key)] = value


@router.post("/documents", status_code=201, response_model=dto.DocumentCreated)
def create_document(
    file: UploadFile = File(...),
    doc_type: Optional[str] = Form(default=None),
    external_ref: Optional[str] = Form(default=None),
    principal: Principal = Depends(require_role("uploader")),
    repo: Repository = Depends(get_repo),
    ingestor: Ingestor = Depends(get_ingestor),
) -> dto.DocumentCreated:
    document_id = new_id("document")
    content = file.file.read()
    upload = UploadInput(
        tenant_id=principal.tenant_id,
        document_id=document_id,
        filename=file.filename or "upload.bin",
        content=content,
        declared_mime=file.content_type,
        doc_type=doc_type,
        external_ref=external_ref,
    )
    try:
        result = ingestor.ingest(upload)
    except IngestError as exc:
        raise ApiError(exc.code, exc.message) from exc

    doc = DocumentRecord(
        id=document_id,
        tenant_id=principal.tenant_id,
        storage_uri=result.storage_uri,
        original_name=file.filename,
        mime_type=result.mime_type,
        page_count=result.page_count,
        doc_type=doc_type,
        external_ref=external_ref,
        status="uploaded",
    )
    pages = [
        PageRecord(
            page_no=p.page_no,
            width=p.width,
            height=p.height,
            image_uri=p.image_uri,
            preproc=p.preproc,
        )
        for p in result.pages
    ]
    repo.create_document(doc, pages)
    return dto.DocumentCreated(
        document_id=document_id, page_count=result.page_count, status="uploaded"
    )


@router.get("/documents", response_model=dto.DocumentList)
def list_documents(
    status: Optional[str] = None,
    cursor: Optional[str] = None,
    limit: int = 50,
    principal: Principal = Depends(require_role("viewer")),
    repo: Repository = Depends(get_repo),
) -> dto.DocumentList:
    rows, next_cursor = repo.list_documents(
        principal.tenant_id, status=status, cursor=cursor, limit=min(limit, 100)
    )
    return dto.DocumentList(
        items=[
            dto.DocumentMeta(
                document_id=d.id,
                status=d.status,
                doc_type=d.doc_type,
                external_ref=d.external_ref,
                page_count=d.page_count,
            )
            for d in rows
        ],
        next_cursor=next_cursor,
    )


def _require_document(repo: Repository, tenant_id: str, document_id: str) -> DocumentRecord:
    doc = repo.get_document(tenant_id, document_id)
    if doc is None:
        raise ApiError("E1001", "ドキュメントが見つかりません", details={"document_id": document_id})
    return doc


@router.get("/documents/{document_id}", response_model=dto.DocumentMeta)
def get_document(
    document_id: str,
    principal: Principal = Depends(require_role("viewer")),
    repo: Repository = Depends(get_repo),
) -> dto.DocumentMeta:
    doc = _require_document(repo, principal.tenant_id, document_id)
    return dto.DocumentMeta(
        document_id=doc.id,
        status=doc.status,
        doc_type=doc.doc_type,
        external_ref=doc.external_ref,
        page_count=doc.page_count,
    )


@router.post("/documents/{document_id}/extract", status_code=202, response_model=dto.ExtractAccepted)
def extract(
    document_id: str,
    body: dto.ExtractRequest,
    request: Request,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    principal: Principal = Depends(require_role("uploader")),
    repo: Repository = Depends(get_repo),
    queue: Queue = Depends(get_queue),
    settings: Settings = Depends(get_settings),
) -> dto.ExtractAccepted:
    _require_document(repo, principal.tenant_id, document_id)

    cached = _idempotency_hit(request, idempotency_key, principal.tenant_id)
    if cached is not None:
        return dto.ExtractAccepted(**cached)

    if repo.has_active_run(principal.tenant_id, document_id):
        raise ApiError("E1005", "実行中の Run と競合しています", details={"document_id": document_id})

    run_id = new_id("run")
    job_id = new_id("job")
    repo.create_run(
        RunRecord(
            id=run_id,
            tenant_id=principal.tenant_id,
            document_id=document_id,
            schema_id=body.schema_id,
            status="processing",
            options=body.options.model_dump(),
        )
    )
    repo.create_job(
        JobRecord(id=job_id, tenant_id=principal.tenant_id, kind="extract", ref_id=run_id)
    )
    repo.set_document_status(principal.tenant_id, document_id, "queued")
    queue.enqueue("q.extract", {"job_id": job_id, "tenant_id": principal.tenant_id, "run_id": run_id})

    payload = {"job_id": job_id, "run_id": run_id}
    _idempotency_store(request, idempotency_key, principal.tenant_id, payload)
    return dto.ExtractAccepted(**payload)


@router.get("/jobs/{job_id}", response_model=dto.JobStatus)
def get_job(
    job_id: str,
    principal: Principal = Depends(require_role("viewer")),
    repo: Repository = Depends(get_repo),
) -> dto.JobStatus:
    job = repo.get_job(principal.tenant_id, job_id)
    if job is None:
        raise ApiError("E1001", "ジョブが見つかりません", details={"job_id": job_id})
    return dto.JobStatus(
        job_id=job.id, kind=job.kind, status=job.status, error_code=job.error_code
    )


@router.get("/documents/{document_id}/result", response_model=dto.ResultResponse)
def get_result(
    document_id: str,
    principal: Principal = Depends(require_role("viewer")),
    repo: Repository = Depends(get_repo),
) -> dto.ResultResponse:
    _require_document(repo, principal.tenant_id, document_id)
    run = repo.get_latest_run(principal.tenant_id, document_id)
    if run is None:
        raise ApiError("E1001", "抽出 Run がありません", details={"document_id": document_id})
    return dto.ResultResponse(
        document_id=document_id,
        run_id=run.id,
        status=run.status,
        result_version=run.result_version,
        engine_versions=run.engine_versions,
        fields=run.fields,
        tables=run.tables,
        review_summary=run.review_summary,
    )


@router.get("/documents/{document_id}/pages/{page_no}/image", response_model=dto.SignedUrl)
def get_page_image(
    document_id: str,
    page_no: int,
    principal: Principal = Depends(require_role("viewer")),
    repo: Repository = Depends(get_repo),
    settings: Settings = Depends(get_settings),
) -> dto.SignedUrl:
    _require_document(repo, principal.tenant_id, document_id)
    pages = repo.get_pages(principal.tenant_id, document_id)
    page = next((p for p in pages if p.page_no == page_no), None)
    if page is None:
        raise ApiError("E1001", "ページが見つかりません", details={"page_no": page_no})
    # TODO: S3 の場合は事前署名 URL を発行（有効期限 signed_url_ttl_sec）
    return dto.SignedUrl(url=page.image_uri, expires_in=settings.signed_url_ttl_sec)


@router.post(
    "/documents/{document_id}/corrections", response_model=dto.CorrectionsAccepted
)
def post_corrections(
    document_id: str,
    body: dto.CorrectionsRequest,
    principal: Principal = Depends(require_role("reviewer")),
    repo: Repository = Depends(get_repo),
) -> dto.CorrectionsAccepted:
    _require_document(repo, principal.tenant_id, document_id)
    run = repo.get_run(principal.tenant_id, body.run_id)
    if run is None or run.document_id != document_id:
        raise ApiError("E1001", "Run が見つかりません", details={"run_id": body.run_id})
    # 楽観ロック（§6.3）: result 取得時 version と不一致なら 409
    if body.version != run.result_version:
        raise ApiError(
            "E1006",
            "楽観ロック競合。最新結果を再取得してください",
            details={"expected": run.result_version, "got": body.version},
        )
    records = [
        CorrectionRecord(
            id=new_id("correction"),
            tenant_id=principal.tenant_id,
            document_id=document_id,
            run_id=body.run_id,
            field_name=item.field_name,
            original_value=item.original_value,
            corrected_value=item.corrected_value,
            note=item.note,
        )
        for item in body.items
    ]
    repo.add_corrections(records)
    # この時点ではグラフを再開しない（confirm で一括反映, §6.3）
    return dto.CorrectionsAccepted(correction_ids=[r.id for r in records])


@router.post(
    "/documents/{document_id}/confirm", status_code=202, response_model=dto.ConfirmAccepted
)
def confirm(
    document_id: str,
    body: dto.ConfirmRequest,
    request: Request,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    principal: Principal = Depends(require_role("reviewer")),
    repo: Repository = Depends(get_repo),
    orchestrator: OrchestratorClient = Depends(get_orchestrator),
) -> dto.ConfirmAccepted:
    _require_document(repo, principal.tenant_id, document_id)
    run = (
        repo.get_run(principal.tenant_id, body.run_id)
        if body.run_id
        else repo.get_latest_run(principal.tenant_id, document_id)
    )
    if run is None:
        raise ApiError("E1001", "Run が見つかりません", details={"document_id": document_id})

    cached = _idempotency_hit(request, idempotency_key, principal.tenant_id)
    if cached is not None:
        return dto.ConfirmAccepted()

    repo.set_document_status(principal.tenant_id, document_id, "in_review")
    orchestrator.resume(run.id, body.overrides)
    _idempotency_store(request, idempotency_key, principal.tenant_id, {"ok": True})
    return dto.ConfirmAccepted()


@router.get("/review/queue", response_model=dto.ReviewQueue)
def review_queue(
    principal: Principal = Depends(require_role("reviewer")),
    repo: Repository = Depends(get_repo),
) -> dto.ReviewQueue:
    runs = repo.list_review_runs(principal.tenant_id)
    items = []
    for run in runs:
        pending = int(run.review_summary.get("pending", 0))
        # 優先度（§8.5 の簡易版）: pending 件数を主指標
        items.append(
            dto.ReviewQueueItem(
                document_id=run.document_id,
                run_id=run.id,
                pending=pending,
                priority=float(pending),
            )
        )
    items.sort(key=lambda i: i.priority, reverse=True)
    return dto.ReviewQueue(items=items)
