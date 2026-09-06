"""REST エンドポイント（§6.2 / §6.3）。"""

from __future__ import annotations

import json
import re
import secrets
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Form, Header, Request, Response, UploadFile
from fastapi.responses import StreamingResponse
from newfan_ingest.storage import page_key
from newfan_netguard import is_blocked_url
from newfan_schemas import resolve_regions
from newfan_workflow import WorkflowGraph, build_candidate, catalog, classify_text, has_errors, lint
from newfan_workflow.lint import Finding
from pydantic import ValidationError

from newfan_gateway import dto
from newfan_gateway.auth import Principal
from newfan_gateway.chat import ChatAgent
from newfan_gateway.config import Settings
from newfan_gateway.admin import AdminRepository, is_activatable
from newfan_gateway.deps import (
    get_admin,
    get_secret_store,
    get_chat_agent,
    get_ingestor,
    get_lock_store,
    get_object_store,
    get_orchestrator,
    get_queue,
    get_repo,
    get_settings,
    get_workflows,
    require_role,
)
from newfan_gateway.errors import ApiError
from newfan_gateway.ids import new_id
from newfan_gateway.locks import DEFAULT_TTL_SEC, LockStore
from newfan_gateway.page_images import (
    issue_page_token,
    presign_s3,
    read_local_image,
    verify_page_token,
)
from newfan_gateway.ports import Ingestor, OrchestratorClient
from newfan_gateway.queue import Queue
from newfan_gateway.workflows_repo import IMPLEMENTED_NODE_TYPES, WorkflowsRepository
from newfan_gateway.records import (
    CorrectionRecord,
    DocumentRecord,
    JobRecord,
    PageRecord,
    RuleRecord,
    RunRecord,
    SchemaFieldDef,
    WorkflowRecord,
    WorkflowRunRecord,
)
from newfan_gateway.repository import DocumentGoneError, Repository
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
    # ページ寸法は**単体取得でのみ**埋める（設計 §6）。一覧 API は DocumentMeta を
    # 共用しており、そちらで埋めると帳票 1 件ごとに pages を引く N+1 になる。
    pages = repo.get_pages(principal.tenant_id, document_id)
    return dto.DocumentMeta(
        document_id=doc.id,
        status=doc.status,
        doc_type=doc.doc_type,
        external_ref=doc.external_ref,
        page_count=doc.page_count,
        pages=[
            dto.PageDim(page_no=p.page_no, width=p.width, height=p.height)
            for p in sorted(pages, key=lambda x: x.page_no)
        ],
    )


@router.delete("/documents/{document_id}", response_model=dto.DocumentDeleted)
def delete_document(
    document_id: str,
    principal: Principal = Depends(require_role("reviewer")),
    repo: Repository = Depends(get_repo),
    wf: WorkflowsRepository = Depends(get_workflows),
    locks: LockStore = Depends(get_lock_store),
    store: Any = Depends(get_object_store),
    settings: Settings = Depends(get_settings),
) -> dto.DocumentDeleted:
    """取り込んだ帳票を消す（原本・ページ画像・抽出結果・学習例まで）。

    復元手段は用意していない。UI 側で必ず確認ダイアログを挟むこと。

    順序は S3 → DB。逆にすると、S3 の削除が失敗したときに storage_uri を失って
    孤児オブジェクトを辿る手段が消える。この順なら失敗しても DB は無傷で、
    帳票は一覧に残ったまま再試行できる。
    """
    doc = _require_document(repo, principal.tenant_id, document_id)

    # 実行中のものを消すと、ワーカーが参照先を失って無限に再配信される。
    reason = repo.get_delete_blocker(
        principal.tenant_id, document_id, stale_minutes=settings.document_delete_stale_minutes
    )
    if reason == "document_busy":
        raise ApiError(
            "E1005",
            "処理中のため削除できません",
            details={"document_id": document_id, "reason": reason, "status": doc.status},
        )
    if reason == "processing":
        raise ApiError(
            "E1005",
            "抽出処理中のため削除できません",
            details={"document_id": document_id, "reason": reason},
        )
    if wf.has_running_workflow_run(principal.tenant_id, document_id):
        raise ApiError(
            "E1005",
            "ワークフロー実行中のため削除できません",
            details={"document_id": document_id, "reason": "workflow_active"},
        )
    # ソフトロックは助言的（§8.2）。gateway は複数タスクで動き InMemoryLockStore は
    # プロセス内なので、これは安全境界ではなく「同僚の作業を踏まない」ための配慮。
    info = locks.get(principal.tenant_id, document_id)
    if info is not None and info.holder_sub != principal.sub:
        raise ApiError(
            "E1005",
            "他のユーザーが確認中です",
            details={
                "document_id": document_id,
                "reason": "locked",
                "holder": info.holder_name,
            },
        )

    prefix = f"{principal.tenant_id}/{document_id}/"
    try:
        objects_deleted = int(store.delete_prefix(prefix))
    except Exception as exc:  # noqa: BLE001 — 実体が残る限り DB は消さない
        raise ApiError(
            "E2000",
            "帳票ファイルの削除に失敗しました。時間をおいて再試行してください。",
            details={"document_id": document_id},
        ) from exc

    try:
        counts = repo.delete_document(
            principal.tenant_id,
            document_id,
            actor_id=principal.sub,
            detail={"objects_deleted": objects_deleted, "prefix": prefix},
        )
    except DocumentGoneError:
        # 同時に 2 回 DELETE した敗者側。実体はもう無いので「見つかりません」で正しい。
        counts = None
    if counts is None:
        # S3 を消したあとで DB から消えていた（同時削除）。実体はもう無いので
        # 「見つかりません」で正しい。
        raise ApiError(
            "E1001", "ドキュメントが見つかりません", details={"document_id": document_id}
        )

    locks.release(principal.tenant_id, document_id, principal.sub)
    return dto.DocumentDeleted(
        document_id=document_id,
        objects_deleted=objects_deleted,
        corrections_deleted=counts.get("corrections_deleted", 0),
        runs_deleted=counts.get("runs_deleted", 0),
    )


@router.post("/documents/{document_id}/classify", response_model=dto.ClassifyResponse)
def classify_document(
    document_id: str,
    principal: Principal = Depends(require_role("viewer")),
    repo: Repository = Depends(get_repo),
    admin: AdminRepository = Depends(get_admin),
) -> dto.ClassifyResponse:
    """帳票種別を推定し、最も近い登録スキーマを提案する（⑦ 抽出UIサジェスト）。

    抽出前はファイル名を信号にした決定論分類（純ロジック newfan_workflow.classify_text）。
    アップロード時に doc_type が明示されていればそれを最優先する。
    """
    doc = _require_document(repo, principal.tenant_id, document_id)
    schemas = admin.list_schemas(principal.tenant_id)
    if not schemas:
        return dto.ClassifyResponse()

    # list_schemas は doc_type ごとの最新版のみを返す（§7.2）
    latest: dict[str, str] = {s.doc_type: s.id for s in schemas}

    # アップロード時に種別指定済みなら、推測より確実なのでそれを採用する
    if doc.doc_type and doc.doc_type in latest:
        return dto.ClassifyResponse(
            suggested_schema_id=latest[doc.doc_type],
            doc_type=doc.doc_type,
            confidence=1.0,
            reason="アップロード時に指定された種別",
            method="declared",
            candidates=[
                dto.ClassifyCandidateDto(schema_id=sid, doc_type=dt, score=0.0)
                for dt, sid in latest.items()
            ],
        )

    candidates = [
        build_candidate(s.doc_type, [f.label or f.name for f in s.fields]) for s in schemas
    ]
    filename = doc.original_name or ""
    outcome = classify_text(text="", filename=filename, candidates=candidates)
    method = "filename" if filename else "heuristic"
    cand_dtos = sorted(
        (
            dto.ClassifyCandidateDto(schema_id=latest.get(dt, ""), doc_type=dt, score=sc)
            for dt, sc in outcome.scores.items()
        ),
        key=lambda c: c.score,
        reverse=True,
    )
    return dto.ClassifyResponse(
        suggested_schema_id=latest.get(outcome.doc_type) if outcome.doc_type else None,
        doc_type=outcome.doc_type,
        confidence=outcome.confidence,
        reason=outcome.reason,
        method=method,
        candidates=cand_dtos,
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
    admin: AdminRepository = Depends(get_admin),
) -> dto.ExtractAccepted:
    _require_document(repo, principal.tenant_id, document_id)

    cached = _idempotency_hit(request, idempotency_key, principal.tenant_id)
    if cached is not None:
        return dto.ExtractAccepted(**cached)

    # 空文字の schema_id は「未指定」として扱う。そのまま INSERT すると
    # extraction_runs の FK 違反で 500（E2000 内部エラー）になり、利用者には
    # 原因が一切見えない（API 直叩きで実際に発生）。存在しない ID も 404 で明示する。
    schema_id = (body.schema_id or "").strip() or None
    if schema_id is not None and admin.get_schema_by_id(principal.tenant_id, schema_id) is None:
        raise ApiError("E1001", "スキーマが見つかりません", details={"schema_id": schema_id})

    # 既定の競合判定は processing + needs_review（外部連携の二重投入防止）。
    # supersede_review=true のときだけ「今まさに処理中」だけを競合とみなす
    # （chat の rerun_extract と同じ意味論）。テンプレート化直後の再抽出は
    # 「自動発見 run が needs_review」が典型状態で、既定のままでは必ず 409 になる。
    if body.supersede_review:
        if repo.has_processing_run(principal.tenant_id, document_id):
            raise ApiError(
                "E1005", "実行中の Run と競合しています", details={"document_id": document_id}
            )
        latest = repo.get_latest_run(principal.tenant_id, document_id)
        if latest is not None and latest.status in ("confirmed", "exported"):
            # 確定済み（会計連携済みを含む）を無警告で置き換えない
            raise ApiError(
                "E1005",
                "確定済みの結果があります。再抽出すると確定値が置き換わります",
                details={"document_id": document_id, "status": latest.status},
            )
        # 新 run を作る前に旧 needs_review を終端させる。残すと get_latest_run・
        # 削除ブロッカー・ワークフローの hitl_gate が古い run を見続ける。
        repo.supersede_review_runs(principal.tenant_id, document_id)
    elif repo.has_active_run(principal.tenant_id, document_id):
        raise ApiError("E1005", "実行中の Run と競合しています", details={"document_id": document_id})

    run_id = new_id("run")
    job_id = new_id("job")
    repo.create_run(
        RunRecord(
            id=run_id,
            tenant_id=principal.tenant_id,
            document_id=document_id,
            schema_id=schema_id,
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
    admin: AdminRepository = Depends(get_admin),
) -> dto.ResultResponse:
    _require_document(repo, principal.tenant_id, document_id)
    run = repo.get_latest_run(principal.tenant_id, document_id)
    if run is None:
        raise ApiError("E1001", "抽出 Run がありません", details={"document_id": document_id})

    # 適用された除外領域と doc_type はスキーマ側から採る。db の 1 SELECT で取る案は
    # 採らない——admin リポジトリなら Pg / InMemory の両実装を通るので、
    # 「InMemory では常に空が返るので UI 実装者が本番との差に気づけない」盲点が消える。
    applied: list[dto.ResolvedRegion] = []
    schema_doc_type: Optional[str] = None
    if run.schema_id:
        rec = admin.get_schema_by_id(principal.tenant_id, run.schema_id)
        if rec is not None:
            schema_doc_type = rec.doc_type
            page_count = len(repo.get_pages(principal.tenant_id, document_id))
            applied = [
                dto.ResolvedRegion(**r)
                for r in resolve_regions(list(rec.exclude_regions), page_count)
            ]

    return dto.ResultResponse(
        document_id=document_id,
        run_id=run.id,
        status=run.status,
        schema_id=run.schema_id,
        result_version=run.result_version,
        engine_versions=run.engine_versions,
        fields=run.fields,
        tables=run.tables,
        review_summary=run.review_summary,
        fallback_pages=run.fallback_pages,
        region_stats=run.region_stats,
        applied_exclude_regions=applied,
        schema_doc_type=schema_doc_type,
    )


@router.get("/documents/{document_id}/pages/{page_no}/image", response_model=dto.SignedUrl)
def get_page_image(
    request: Request,
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

    # 保管先 URI（file:// / s3://）をそのまま返すとブラウザが読めず検証画面の帳票が
    # 表示されない（実アップロード経路で検出。dev seed は data: URI だったため露見しなかった）。
    ttl = settings.signed_url_ttl_sec
    if settings.s3_bucket:
        url = presign_s3(
            settings.s3_bucket,
            page_key(principal.tenant_id, document_id, page_no),
            ttl_sec=ttl,
        )
    else:
        token = issue_page_token(
            tenant_id=principal.tenant_id,
            document_id=document_id,
            page_no=page_no,
            jwt_secret=settings.jwt_secret,
            jwt_alg=settings.jwt_alg,
            ttl_sec=ttl,
        )
        base = str(request.base_url).rstrip("/")
        url = f"{base}/v1/documents/{document_id}/pages/{page_no}/content?token={token}"
    return dto.SignedUrl(url=url, expires_in=ttl)


@router.get("/documents/{document_id}/pages/{page_no}/content")
def get_page_image_content(
    document_id: str,
    page_no: int,
    token: str,
    repo: Repository = Depends(get_repo),
    settings: Settings = Depends(get_settings),
) -> Response:
    """署名URLの実体配信。<img src> は Authorization を付けられないため token で認可する。"""
    tenant_id = verify_page_token(
        token,
        document_id=document_id,
        page_no=page_no,
        jwt_secret=settings.jwt_secret,
        jwt_alg=settings.jwt_alg,
    )
    page = next(
        (p for p in repo.get_pages(tenant_id, document_id) if p.page_no == page_no), None
    )
    if page is None:
        raise ApiError("E1001", "ページが見つかりません", details={"page_no": page_no})
    data = read_local_image(page.image_uri, storage_root=settings.storage_root)
    return Response(content=data, media_type="image/png")


# ============ 検証画面ソフトロック（§8.2） ============


def _lock_status(document_id: str, me: str, info: Any, held_by_me: bool) -> dto.LockStatus:
    if info is None:
        return dto.LockStatus(document_id=document_id, locked=False, held_by_me=False)
    return dto.LockStatus(
        document_id=document_id,
        locked=True,
        held_by_me=held_by_me,
        holder=info.holder_name,
        remaining_sec=info.remaining_sec(),
        ttl_sec=DEFAULT_TTL_SEC,
    )


@router.post("/documents/{document_id}/lock", response_model=dto.LockStatus)
def acquire_lock(
    document_id: str,
    principal: Principal = Depends(require_role("reviewer")),
    repo: Repository = Depends(get_repo),
    locks: LockStore = Depends(get_lock_store),
) -> dto.LockStatus:
    """ソフトロックを取得/更新する（マウント時・ハートビート）。

    他者が保持中なら acquired=False（held_by_me=False）で現保持者を返す。助言的なので
    HTTP は常に 200（バナー表示はクライアント側で held_by_me により判断, §8.2）。
    """
    _require_document(repo, principal.tenant_id, document_id)
    acquired, info = locks.acquire(principal.tenant_id, document_id, principal.sub)
    return _lock_status(document_id, principal.sub, info, held_by_me=acquired)


@router.get("/documents/{document_id}/lock", response_model=dto.LockStatus)
def get_lock(
    document_id: str,
    principal: Principal = Depends(require_role("reviewer")),
    repo: Repository = Depends(get_repo),
    locks: LockStore = Depends(get_lock_store),
) -> dto.LockStatus:
    """現在のロック状態を返す（ポーリング用）。"""
    _require_document(repo, principal.tenant_id, document_id)
    info = locks.get(principal.tenant_id, document_id)
    held_by_me = info is not None and info.holder_sub == principal.sub
    return _lock_status(document_id, principal.sub, info, held_by_me=held_by_me)


@router.delete("/documents/{document_id}/lock", response_model=dto.LockStatus)
def release_lock(
    document_id: str,
    principal: Principal = Depends(require_role("reviewer")),
    repo: Repository = Depends(get_repo),
    locks: LockStore = Depends(get_lock_store),
) -> dto.LockStatus:
    """保持者本人によるロック解放（アンマウント・確定時）。"""
    _require_document(repo, principal.tenant_id, document_id)
    locks.release(principal.tenant_id, document_id, principal.sub)
    return dto.LockStatus(document_id=document_id, locked=False, held_by_me=False)


@router.post(
    "/documents/{document_id}/corrections", response_model=dto.CorrectionsAccepted
)
def post_corrections(
    document_id: str,
    body: dto.CorrectionsRequest,
    principal: Principal = Depends(require_role("reviewer")),
    repo: Repository = Depends(get_repo),
) -> dto.CorrectionsAccepted:
    doc = _require_document(repo, principal.tenant_id, document_id)
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
    # doc_type/supplier_key/context は learn ノードが memory へ渡す検索キーと embedding 入力
    # （DD-06/DD-07）。埋めないと修正が記録されても次回の抽出に効かない。
    records = [
        CorrectionRecord(
            id=new_id("correction"),
            tenant_id=principal.tenant_id,
            document_id=document_id,
            run_id=body.run_id,
            field_name=item.field_name,
            original_value=item.original_value,
            corrected_value=item.corrected_value,
            doc_type=doc.doc_type,
            supplier_key=item.supplier_key,
            context=item.context or item.note,
            reviewer_id=principal.sub,
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
    locks: LockStore = Depends(get_lock_store),
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

    # §3.2: confirm は「随時保存した修正」を feedback としてグラフへ渡す。
    # apply_feedback が確定値をマージし、learn が memory へ登録する（DD-06/DD-07）。
    # ここで渡さないと、修正が correction_logs に残るだけで学習ループに何も伝わらない
    # （実 AWS で memory 0 件のまま confirmed になるのを確認）。
    saved = repo.list_corrections(principal.tenant_id, run.id)
    feedback: dict[str, Any] = {
        "corrections": [
            {
                "field_name": c.field_name,
                "original_value": c.original_value,
                "corrected_value": c.corrected_value,
                "supplier_key": c.supplier_key,
                "context": c.context,
            }
            for c in saved
        ]
    }
    if body.overrides:
        feedback["overrides"] = body.overrides

    repo.set_document_status(principal.tenant_id, document_id, "in_review")
    # ワークフロー起点の Run は options に notify 先を持つ（§16 P3 ensure_extract_run）。
    # 確定フローの再開ジョブへ中継すると、finalize 完了時にワーカーが confirm_done を
    # q.workflow へ積み、hitl_gate で待つワークフローが下流継続する（§16 §8 / P5）。
    orchestrator.resume(
        run.id, principal.tenant_id, feedback,
        notify=(run.options or {}).get("workflow_notify"),
    )
    locks.release(principal.tenant_id, document_id, principal.sub)  # 確定で待機者を解放
    _idempotency_store(request, idempotency_key, principal.tenant_id, {"ok": True})
    return dto.ConfirmAccepted()


@router.get("/review/queue", response_model=dto.ReviewQueue)
def review_queue(
    principal: Principal = Depends(require_role("reviewer")),
    repo: Repository = Depends(get_repo),
) -> dto.ReviewQueue:
    runs = repo.list_review_runs(principal.tenant_id)
    # hitl_gate の priority_boost（workflow_runs.waiting）を加点する（§16 P5）
    boosts = repo.list_hitl_boosts(principal.tenant_id)
    items = []
    for run in runs:
        pending = int(run.review_summary.get("pending", 0))
        # 優先度（§8.5 の簡易版）: pending 件数を主指標 + ワークフローの boost
        items.append(
            dto.ReviewQueueItem(
                document_id=run.document_id,
                run_id=run.id,
                pending=pending,
                priority=float(pending + boosts.get(run.document_id or "", 0)),
            )
        )
    items.sort(key=lambda i: i.priority, reverse=True)
    return dto.ReviewQueue(items=items)


# ============ 管理画面（SCR-04/05/06, admin） ============


def _schema_dto(rec: Any) -> dto.SchemaDto:
    return dto.SchemaDto(
        id=rec.id,
        doc_type=rec.doc_type,
        version=rec.version,
        fields=[dto.SchemaFieldDto(**f.model_dump()) for f in rec.fields],
        # 応答忠実性がこの機能の生命線（設計 §6）。ここが欠けると旧編集画面の
        # 「取得 → 編集 → 新版として保存」往復で region / exclude が全滅する。
        exclude_regions=list(getattr(rec, "exclude_regions", []) or []),
        source_page_count=getattr(rec, "source_page_count", None),
    )


def _rule_dto(rec: Any) -> dto.RuleDto:
    return dto.RuleDto(
        id=rec.id,
        doc_type=rec.doc_type,
        supplier_key=rec.supplier_key,
        field_name=rec.field_name,
        rule_type=rec.rule_type,
        rule_json=rec.rule_json,
        status=rec.status,
        validation_report=rec.validation_report,
        source_correction_ids=rec.source_correction_ids,
        created_by=rec.created_by,
        # llm_hint の検証免除は「人が明示的に書いた指示」に限る。学習エージェント生成の
        # llm_hint（created_by="agent"）まで免除すると、検証不合格のルールが1クリックで
        # 全 KIE プロンプトに注入される（レビュー保留所見）。regex/vocab は従来どおり
        # 再現率ゲートが必要。
        activatable=(rec.rule_type == "llm_hint" and rec.created_by != "agent")
        or is_activatable(rec.validation_report),
    )


@router.get("/schemas", response_model=dto.SchemaList)
def list_schemas(
    principal: Principal = Depends(require_role("admin")),
    admin: AdminRepository = Depends(get_admin),
) -> dto.SchemaList:
    return dto.SchemaList(items=[_schema_dto(s) for s in admin.list_schemas(principal.tenant_id)])


@router.get("/schemas/{doc_type}", response_model=dto.SchemaDto)
def get_schema(
    doc_type: str,
    principal: Principal = Depends(require_role("admin")),
    admin: AdminRepository = Depends(get_admin),
) -> dto.SchemaDto:
    rec = admin.get_schema(principal.tenant_id, doc_type)
    if rec is None:
        raise ApiError("E1001", "スキーマが見つかりません", details={"doc_type": doc_type})
    return _schema_dto(rec)


@router.put("/schemas", response_model=dto.SchemaDto)
def put_schema(
    body: dto.PutSchemaRequest,
    principal: Principal = Depends(require_role("admin")),
    admin: AdminRepository = Depends(get_admin),
) -> dto.SchemaDto:
    # 新規作成モードはサーバ側で重複を拒否する。クライアントの重複チェックは一覧が
    # 陳腐化していると素通りし、既存スキーマを黙って新版で置換してしまう（レビュー確定）。
    if body.create and admin.get_schema(principal.tenant_id, body.doc_type) is not None:
        raise ApiError(
            "E1005",
            "同名のスキーマが既に存在します。既存スキーマを選んで編集してください",
            details={"doc_type": body.doc_type},
        )
    # RegionRect 自体の形式（0..1 / x1<x2 / 面積）は pydantic が検証済み。ここでは
    # **文脈依存の制約**だけを見る: include（fields[].region）に page:null は許さない。
    # 「どのページのどこを読むか」の指定にならず、全ページに同座標を当てる意図とも
    # 区別できないため。exclude は page:null（全ページ）が正当な指定。
    for f in body.fields:
        if f.region is not None and f.region.page is None:
            raise ApiError(
                "E1003",
                "読取領域にはページ指定が必要です（全ページ指定は除外領域のみ）",
                details={"field": f.name},
            )
    fields = [SchemaFieldDef(**f.model_dump()) for f in body.fields]
    # exclude_regions / source_page_count は None のまま渡す（= 直前版から引き継ぎ）。
    # 旧編集画面・chat 経路はこれらを送らないので、ここで [] に潰すと保存 1 回で
    # 除外設定が消える（設計 §4.4）。
    rec = admin.put_schema(  # 常に新版
        principal.tenant_id,
        body.doc_type,
        fields,
        exclude_regions=body.exclude_regions,
        source_page_count=body.source_page_count,
    )
    return _schema_dto(rec)


# ---------- ワークフロー実行（§16 設計 v0.2 §11 / P3） ----------

WORKFLOW_STREAM = "q.workflow"


def _run_summary(rec: WorkflowRunRecord) -> dto.WorkflowRunSummaryDto:
    return dto.WorkflowRunSummaryDto(
        id=rec.id,
        workflow_id=rec.workflow_id,
        workflow_version=rec.workflow_version,
        document_id=rec.document_id,
        status=rec.status,
        error=rec.error,
        started_at=rec.started_at,
        finished_at=rec.finished_at,
    )


@router.post(
    "/workflows/{workflow_id}/runs", status_code=202, response_model=dto.WorkflowRunAccepted
)
def start_workflow_run(
    workflow_id: str,
    body: dto.WorkflowRunRequest,
    principal: Principal = Depends(require_role("uploader")),
    wf: WorkflowsRepository = Depends(get_workflows),
    repo: Repository = Depends(get_repo),
    queue: Queue = Depends(get_queue),
) -> dto.WorkflowRunAccepted:
    """手動実行（source.manual, §7.1）。UI アップロード起点・API 起点の両方がこれに乗る。"""
    rec = wf.get_workflow(principal.tenant_id, workflow_id)
    if rec is None:
        raise ApiError("E1001", "ワークフローが見つかりません", details={"workflow_id": workflow_id})
    if rec.status != "active":
        # 有効化＝版の固定（§11.1）。draft のまま動かすと「編集途中の定義が走る」事故になる
        raise ApiError(
            "E1005", "active でないワークフローは実行できません", details={"status": rec.status}
        )
    _require_document(repo, principal.tenant_id, body.document_id)

    # 発火トリガー＝グラフの source.manual ノード。複数トリガーの WF では runner が
    # この node_id の経路だけを実行する（無い場合は従来どおり全経路＝後方互換）
    manual_node_id = next(
        (
            n.get("id")
            for n in (rec.graph_json or {}).get("nodes", [])
            if n.get("type") == "source.manual"
        ),
        None,
    )
    run = wf.create_run(
        WorkflowRunRecord(
            id=new_id("wfrun"),
            tenant_id=principal.tenant_id,
            workflow_id=workflow_id,
            workflow_version=rec.version,
            document_id=body.document_id,
            # graph_json のスナップショットで版を固定する（§11.1）。以後 workflows 側が
            # 更新されても、この run は開始時点の定義で最後まで走る
            trigger={
                "type": "manual",
                "by": principal.sub,
                "node_id": manual_node_id,
                "graph_json": rec.graph_json,
            },
        )
    )
    queue.enqueue(
        WORKFLOW_STREAM,
        {"type": "start", "tenant_id": principal.tenant_id, "workflow_run_id": run.id},
    )
    return dto.WorkflowRunAccepted(workflow_run_id=run.id, workflow_version=rec.version)


@router.get("/workflows/{workflow_id}/runs", response_model=dto.WorkflowRunList)
def list_workflow_runs(
    workflow_id: str,
    status: Optional[str] = None,
    limit: int = 50,
    principal: Principal = Depends(require_role("viewer")),
    wf: WorkflowsRepository = Depends(get_workflows),
) -> dto.WorkflowRunList:
    rows = wf.list_runs(principal.tenant_id, workflow_id, status=status, limit=min(limit, 200))
    return dto.WorkflowRunList(items=[_run_summary(r) for r in rows])


@router.get("/workflow-runs/{run_id}", response_model=dto.WorkflowRunDto)
def get_workflow_run(
    run_id: str,
    principal: Principal = Depends(require_role("viewer")),
    wf: WorkflowsRepository = Depends(get_workflows),
) -> dto.WorkflowRunDto:
    rec = wf.get_run(principal.tenant_id, run_id)
    if rec is None:
        raise ApiError("E1001", "workflow run が見つかりません", details={"run_id": run_id})
    node_runs = wf.list_node_runs(principal.tenant_id, run_id)
    return dto.WorkflowRunDto(
        **_run_summary(rec).model_dump(),
        waiting=(rec.state or {}).get("waiting"),
        node_runs=[dto.WorkflowNodeRunDto(**n.model_dump()) for n in node_runs],
    )


@router.post("/workflow-runs/{run_id}/retry", status_code=202, response_model=dto.WorkflowRunAccepted)
def retry_workflow_run(
    run_id: str,
    principal: Principal = Depends(require_role("admin")),
    wf: WorkflowsRepository = Depends(get_workflows),
    queue: Queue = Depends(get_queue),
) -> dto.WorkflowRunAccepted:
    """失敗セグメントからの再実行（§6.5）。checkpoint から続きが走り、完了済みノードは
    再実行されない（実測済みの再実行境界）。"""
    rec = wf.get_run(principal.tenant_id, run_id)
    if rec is None:
        raise ApiError("E1001", "workflow run が見つかりません", details={"run_id": run_id})
    if rec.status != "failed":
        raise ApiError(
            "E1005", "failed でない run は retry できません", details={"status": rec.status}
        )
    queue.enqueue(
        WORKFLOW_STREAM,
        {"type": "retry", "tenant_id": principal.tenant_id, "workflow_run_id": run_id},
    )
    wf.record_audit(
        principal.tenant_id,
        actor_id=principal.sub,
        action="workflow.retry",
        target_id=run_id,
        detail={"workflow_id": rec.workflow_id},
    )
    return dto.WorkflowRunAccepted(
        workflow_run_id=run_id, workflow_version=rec.workflow_version
    )


# ---------- ワークフロー管理（§16 設計 v0.2 §11 / P2） ----------
# 注意: /workflows/catalog は /workflows/{workflow_id} より先に登録すること。
# FastAPI は登録順にマッチするため、後にすると "catalog" が id として解釈される。


def _validate_graph(data: dict[str, Any]) -> "WorkflowGraph":
    """graph_json をモデル検証する。不正は 422（E4001 スキーマ不正）。

    未知のノード種別・config の typo・不正な条件式は保存の瞬間に断る（§4.1）。
    実行時に初めて落ちると、有効化済みのワークフローが本番で死ぬ。
    """
    try:
        return WorkflowGraph.model_validate(data)
    except ValidationError as exc:
        errors = [
            {"loc": ".".join(str(p) for p in e["loc"]), "msg": e["msg"], "type": e["type"]}
            for e in exc.errors(include_url=False)[:20]
        ]
        raise ApiError("E4001", "graph_json がスキーマに合いません", details={"errors": errors}) from exc


def _lint_workflow(
    rec: "WorkflowRecord", graph: "WorkflowGraph", wf: WorkflowsRepository, tenant_id: str
) -> tuple[list[Finding], list[str]]:
    findings = lint(
        graph,
        auto_confirm=rec.auto_confirm,
        schema_exists=lambda sid: wf.schema_exists(tenant_id, sid),
        connection_ok=lambda cid: wf.connection_ok(tenant_id, cid),
        schema_is_latest=lambda sid: wf.schema_is_latest(tenant_id, sid),
    )
    unsupported = sorted(str(t) for t in {n.type for n in graph.nodes} - IMPLEMENTED_NODE_TYPES)
    return findings, unsupported


def _workflow_dto(rec: "WorkflowRecord") -> dto.WorkflowDto:
    return dto.WorkflowDto(
        id=rec.id,
        name=rec.name,
        status=rec.status,
        version=rec.version,
        auto_confirm=rec.auto_confirm,
        updated_at=rec.updated_at,
        graph_json=rec.graph_json,
    )


@router.get("/workflows/catalog")
def workflow_catalog(
    principal: Principal = Depends(require_role("admin")),
) -> dict[str, Any]:
    """ノード種別 → config の JSON Schema（SCR-07 のフォーム自動生成用, §4.4）。"""
    return {"types": catalog(), "implemented": sorted(IMPLEMENTED_NODE_TYPES)}


@router.get("/workflows", response_model=dto.WorkflowList)
def list_workflows(
    principal: Principal = Depends(require_role("admin")),
    wf: WorkflowsRepository = Depends(get_workflows),
) -> dto.WorkflowList:
    rows = wf.list_workflows(principal.tenant_id)
    return dto.WorkflowList(
        items=[dto.WorkflowSummaryDto(**_workflow_dto(r).model_dump(exclude={"graph_json"})) for r in rows]
    )


@router.post("/workflows", status_code=201, response_model=dto.WorkflowDto)
def create_workflow(
    body: dto.WorkflowCreateRequest,
    principal: Principal = Depends(require_role("admin")),
    wf: WorkflowsRepository = Depends(get_workflows),
) -> dto.WorkflowDto:
    graph = _validate_graph(body.graph_json)
    rec = wf.create_workflow(
        WorkflowRecord(
            id=new_id("workflow"),
            tenant_id=principal.tenant_id,
            name=body.name,
            graph_json=graph.model_dump(by_alias=True, exclude_none=True),
            auto_confirm=body.auto_confirm,
            created_by=principal.sub,
        )
    )
    wf.record_audit(
        principal.tenant_id,
        actor_id=principal.sub,
        action="workflow.create",
        target_id=rec.id,
        detail={"version": rec.version, "name": rec.name},
    )
    return _workflow_dto(rec)


@router.get("/workflows/{workflow_id}", response_model=dto.WorkflowDto)
def get_workflow(
    workflow_id: str,
    principal: Principal = Depends(require_role("admin")),
    wf: WorkflowsRepository = Depends(get_workflows),
) -> dto.WorkflowDto:
    rec = wf.get_workflow(principal.tenant_id, workflow_id)
    if rec is None:
        raise ApiError("E1001", "ワークフローが見つかりません", details={"workflow_id": workflow_id})
    return _workflow_dto(rec)


@router.put("/workflows/{workflow_id}", response_model=dto.WorkflowDto)
def update_workflow(
    workflow_id: str,
    body: dto.WorkflowUpdateRequest,
    principal: Principal = Depends(require_role("admin")),
    wf: WorkflowsRepository = Depends(get_workflows),
) -> dto.WorkflowDto:
    graph = _validate_graph(body.graph_json)
    rec = wf.update_workflow(
        principal.tenant_id,
        workflow_id,
        graph_json=graph.model_dump(by_alias=True, exclude_none=True),
        name=body.name,
        auto_confirm=body.auto_confirm,
    )
    if rec is None:
        raise ApiError("E1001", "ワークフローが見つかりません", details={"workflow_id": workflow_id})
    wf.record_audit(
        principal.tenant_id,
        actor_id=principal.sub,
        action="workflow.update",
        target_id=rec.id,
        detail={"version": rec.version},
    )
    return _workflow_dto(rec)


@router.post("/workflows/{workflow_id}/lint", response_model=dto.WorkflowLintResponse)
def lint_workflow(
    workflow_id: str,
    body: Optional[dto.WorkflowLintRequest] = None,
    principal: Principal = Depends(require_role("admin")),
    wf: WorkflowsRepository = Depends(get_workflows),
) -> dto.WorkflowLintResponse:
    rec = wf.get_workflow(principal.tenant_id, workflow_id)
    if rec is None:
        raise ApiError("E1001", "ワークフローが見つかりません", details={"workflow_id": workflow_id})
    graph_data = body.graph_json if (body and body.graph_json is not None) else rec.graph_json
    graph = _validate_graph(graph_data)
    findings, unsupported = _lint_workflow(rec, graph, wf, principal.tenant_id)
    return dto.WorkflowLintResponse(
        findings=[dto.LintFindingDto(**f.__dict__) for f in findings],
        activatable=not has_errors(findings) and not unsupported,
        unsupported_types=unsupported,
    )


def _sink_previews(graph: Any, tenant_id: str, admin: AdminRepository) -> list[dto.SinkPreviewDto]:
    from newfan_gateway.dryrun import preview_sinks

    previews = preview_sinks(graph, lambda cid: admin.get_connection(tenant_id, cid))
    return [
        dto.SinkPreviewDto(
            node_id=p.node_id, node_type=p.node_type, ok=p.ok,
            connection_id=p.connection_id, sql=p.sql, payload=p.payload,
            columns=p.columns, error=p.error,
        )
        for p in previews
    ]


@router.post("/workflows/{workflow_id}/dry-run", response_model=dto.DryRunResult)
def dry_run_workflow(
    workflow_id: str,
    principal: Principal = Depends(require_role("admin")),
    wf: WorkflowsRepository = Depends(get_workflows),
    admin: AdminRepository = Depends(get_admin),
) -> dto.DryRunResult:
    """dry-run（sink プレビュー, §9 / P6）。

    sink は実行しない。db_write の SQL は実行側と同一実装（dbsink）で生成するため
    「プレビュー = 実 SQL」。db_write を含むワークフローの有効化はこれの成功が前提。
    """
    rec = wf.get_workflow(principal.tenant_id, workflow_id)
    if rec is None:
        raise ApiError("E1001", "ワークフローが見つかりません", details={"workflow_id": workflow_id})
    graph = _validate_graph(rec.graph_json)
    sinks = _sink_previews(graph, principal.tenant_id, admin)
    ok = all(p.ok for p in sinks)
    wf.record_audit(
        principal.tenant_id, actor_id=principal.sub, action="workflow.dry_run",
        target_id=workflow_id, detail={"ok": ok},
    )
    return dto.DryRunResult(ok=ok, sinks=sinks)


@router.post("/workflows/{workflow_id}/activate", response_model=dto.WorkflowDto)
def activate_workflow(
    workflow_id: str,
    principal: Principal = Depends(require_role("admin")),
    wf: WorkflowsRepository = Depends(get_workflows),
    admin: AdminRepository = Depends(get_admin),
) -> dto.WorkflowDto:
    """有効化＝版の固定（§11.1）。lint error ゼロ + 実装済みノードのみが条件。"""
    rec = wf.get_workflow(principal.tenant_id, workflow_id)
    if rec is None:
        raise ApiError("E1001", "ワークフローが見つかりません", details={"workflow_id": workflow_id})
    graph = _validate_graph(rec.graph_json)

    findings, unsupported = _lint_workflow(rec, graph, wf, principal.tenant_id)
    if unsupported:
        # 保存と lint は 13 種すべて通すが、実行できないノードの有効化はここで断る。
        # 「エディタに置けるのに動かない」を有効化の境界で明示する（§4.2）。
        raise ApiError(
            "E4001",
            "未実装のノード種別が含まれています（有効化できません）",
            details={"unsupported_types": unsupported},
        )
    errors = [f for f in findings if f.severity == "error"]
    if errors:
        raise ApiError(
            "E4001",
            "構成 lint にエラーがあります（有効化できません）",
            details={"findings": [f.__dict__ for f in errors]},
        )

    # P6: db_write を含むグラフは dry-run 成功が有効化の前提（§8 / DD-12）。
    # webhook のみのグラフには課さない（接続実在は L010 が担保）
    from newfan_workflow.models import DbWriteNode

    if any(isinstance(n, DbWriteNode) for n in graph.nodes):
        sinks = _sink_previews(graph, principal.tenant_id, admin)
        bad = [p for p in sinks if p.node_type == "sink.db_write" and not p.ok]
        if bad:
            raise ApiError(
                "E4001",
                "dry-run が失敗しました（有効化できません）",
                details={"dry_run": [p.model_dump() for p in bad]},
            )

    updated = wf.set_status(principal.tenant_id, workflow_id, "active")
    assert updated is not None  # 直前に取得済み
    wf.record_audit(
        principal.tenant_id,
        actor_id=principal.sub,
        action="workflow.activate",
        target_id=workflow_id,
        detail={"version": updated.version,
                "warnings": [f.__dict__ for f in findings if f.severity == "warning"]},
    )
    return _workflow_dto(updated)


@router.post("/workflows/{workflow_id}/pause", response_model=dto.WorkflowDto)
def pause_workflow(
    workflow_id: str,
    principal: Principal = Depends(require_role("admin")),
    wf: WorkflowsRepository = Depends(get_workflows),
) -> dto.WorkflowDto:
    rec = wf.get_workflow(principal.tenant_id, workflow_id)
    if rec is None:
        raise ApiError("E1001", "ワークフローが見つかりません", details={"workflow_id": workflow_id})
    if rec.status != "active":
        raise ApiError(
            "E1005", "active でないワークフローは停止できません", details={"status": rec.status}
        )
    updated = wf.set_status(principal.tenant_id, workflow_id, "paused")
    assert updated is not None
    wf.record_audit(
        principal.tenant_id,
        actor_id=principal.sub,
        action="workflow.pause",
        target_id=workflow_id,
        detail={"version": updated.version},
    )
    return _workflow_dto(updated)


_CONNECTION_TYPES = {"postgres", "webhook", "s3", "gdrive", "m365", "box"}
# フォルダ監視系（⑤⑥）。config.folder_id と「今すぐ同期」を同型で扱う
_FOLDER_SOURCE_TYPES = {"gdrive", "m365", "box"}
# 秘密らしいキーの部分一致判定に使う（完全一致だと passwd/secret_access_key 等が抜ける）
_SECRETY_SUBSTRINGS = ("secret", "password", "passwd", "pwd", "token", "api_key", "apikey",
                       "credential")


def _find_secrety_keys(obj: Any, path: str = "") -> list[str]:
    """config 内の秘密らしいキーを**再帰的に**探す（ネスト 1 段で回避されないように）。"""
    found: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            kp = f"{path}.{k}" if path else str(k)
            if any(sub in str(k).lower() for sub in _SECRETY_SUBSTRINGS):
                found.append(kp)
            found.extend(_find_secrety_keys(v, kp))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            found.extend(_find_secrety_keys(v, f"{path}[{i}]"))
    return found


def _redact_config(obj: Any) -> Any:
    """API 応答用の再帰マスク。旧 webhook 行など既存の平文秘密を外に出さない。"""
    if isinstance(obj, dict):
        return {
            k: ("***" if any(sub in str(k).lower() for sub in _SECRETY_SUBSTRINGS)
                else _redact_config(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact_config(v) for v in obj]
    return obj


@router.get("/connections", response_model=dto.ConnectionList)
def list_connections(
    principal: Principal = Depends(require_role("admin")),
    admin: AdminRepository = Depends(get_admin),
) -> dto.ConnectionList:
    rows = admin.list_connections(principal.tenant_id)
    return dto.ConnectionList(
        items=[
            dto.ConnectionDto(
                id=r.id, type=r.type, name=r.name,
                # 旧 webhook 行の config.secret（平文）を API に出さない（再帰マスク）
                config=_redact_config(r.config or {}),
                secret_ref=r.secret_ref, allowed_tables=r.allowed_tables,
                status=r.status, created_at=r.created_at,
                last_synced_at=r.last_synced_at,
                last_sync_status=r.last_sync_status,
                last_sync_error=r.last_sync_error,
            )
            for r in rows
        ]
    )


@router.post("/connections", status_code=201, response_model=dto.ConnectionDto)
def create_connection(
    body: dto.ConnectionCreateRequest,
    principal: Principal = Depends(require_role("admin")),
    admin: AdminRepository = Depends(get_admin),
    wf: WorkflowsRepository = Depends(get_workflows),
) -> dto.ConnectionDto:
    """接続の登録（§16.5 / P6）。

    秘密は受け取らない。利用者が Secrets Manager（ai-ocr/<env>/conn/ 配下）に登録し、
    secret_ref（ARN）だけを渡す（LLM キーと同じ運用）。config に秘密が紛れたら断る。
    """
    if body.type not in _CONNECTION_TYPES:
        raise ApiError(
            "E4001", "未対応の接続種別です",
            details={"type": body.type, "supported": sorted(_CONNECTION_TYPES)},
        )
    leaked = _find_secrety_keys(body.config)
    if leaked:
        raise ApiError(
            "E4001",
            "config に秘密を入れてはいけません（Secrets Manager に置いて secret_ref を渡す, §16.5）",
            details={"keys": leaked},
        )
    # secret_ref はテナントの名前空間（.../conn/<tenant_id>/...）内だけを許す。
    # これが無いと他テナントの秘密名/ARN を自分の接続に張り、自分の config.host へ
    # パスワードとして送出させられる（クロステナント窃取。レビューで実証）。
    # 例外: フォルダ監視系（gdrive/m365/box）の `env:NAME` はローカル/compose の
    # 実 OAuth 検証用に許可する。これらの秘密は固定の各社トークンエンドポイントへ
    # しか送られない（利用者が宛先を差し替えられる db_write とは攻撃面が異なる）
    env_ref_ok = body.type in _FOLDER_SOURCE_TYPES and (body.secret_ref or "").startswith("env:")
    if body.secret_ref and not env_ref_ok and f"/conn/{principal.tenant_id}/" not in body.secret_ref:
        raise ApiError(
            "E4001",
            "secret_ref は自テナントの名前空間にある必要があります"
            f"（ai-ocr/<env>/conn/{principal.tenant_id}/<名前> で登録して ARN か名前を渡す）",
            details={"secret_ref": body.secret_ref},
        )
    # フォルダ監視系（gdrive/m365/box）は監視フォルダが無いと何も検知できない
    # （サイレント故障）ため作成時に要求する
    if body.type in _FOLDER_SOURCE_TYPES:
        folder_id = str(body.config.get("folder_id") or "").strip()
        if not folder_id:
            raise ApiError(
                "E4001",
                f"{body.type} 接続には config.folder_id（監視するフォルダの ID）が必要です",
                details={"config_keys": sorted(body.config.keys())},
            )
        # コピペ由来の改行・クォート等は SaaS 側クエリを破壊し、同期がサイレントに
        # 失敗し続ける（レビュー確定）。作成時に断り、値は正規化して保存
        if len(folder_id) > 200 or any(ch in folder_id for ch in ("'", '"', "\n", "\r", "\t")):
            raise ApiError(
                "E4001",
                "folder_id に使えない文字が含まれています（クォート・改行・タブ不可、200文字以内）",
                details={"folder_id_length": len(folder_id)},
            )
        body.config = {**body.config, "folder_id": folder_id}
    table_re = r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$"
    bad_tables = [t for t in body.allowed_tables if not re.match(table_re, t)]
    if bad_tables:
        raise ApiError("E4001", "allowed_tables が識別子ではありません", details={"tables": bad_tables})
    rec = admin.create_connection(
        principal.tenant_id, type=body.type, name=body.name, config=body.config,
        secret_ref=body.secret_ref, allowed_tables=body.allowed_tables,
    )
    wf.record_audit(
        principal.tenant_id, actor_id=principal.sub, action="connection.create",
        target_id=rec.id, detail={"type": body.type, "allowed_tables": body.allowed_tables},
    )
    return dto.ConnectionDto(
        id=rec.id, type=rec.type, name=rec.name, config=rec.config,
        secret_ref=rec.secret_ref, allowed_tables=rec.allowed_tables,
        status=rec.status, created_at=rec.created_at,
    )


@router.post("/connections/{connection_id}/test", response_model=dto.ConnectionTestResult)
def test_connection(
    connection_id: str,
    principal: Principal = Depends(require_role("admin")),
    admin: AdminRepository = Depends(get_admin),
    wf: WorkflowsRepository = Depends(get_workflows),
    secret_store: Any = Depends(get_secret_store),
) -> dto.ConnectionTestResult:
    """疎通テスト（DB は SELECT 1 のみ, §16.5 / P6）。成功で status='tested' になる。

    sink/トリガーは tested/active の接続しか使わないため、これを通すまで実行に乗らない。
    """
    rec = admin.get_connection(principal.tenant_id, connection_id)
    if rec is None:
        raise ApiError("E1001", "接続が見つかりません", details={"connection_id": connection_id})
    if rec.type != "postgres":
        raise ApiError(
            "E1005", "疎通テストは type=postgres のみ対応です", details={"type": rec.type}
        )

    from newfan_workflow.dbsink import DbSinkError, build_dsn

    try:
        secret = None
        if rec.secret_ref:
            if secret_store is None:
                raise DbSinkError("secret_ref があるのに秘密の保管先が未配線です")
            secret = secret_store.get(rec.secret_ref)
        dsn = build_dsn(rec.config, secret)
        import psycopg

        with psycopg.connect(dsn, connect_timeout=5) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:  # noqa: BLE001 - 失敗理由を利用者へ返す
        # DSN パースエラー等の例外文言には秘密の断片が混ざり得るため必ずマスクする
        msg = str(exc)
        if secret:
            msg = msg.replace(secret, "***")
        wf.record_audit(
            principal.tenant_id, actor_id=principal.sub, action="connection.test",
            target_id=connection_id, detail={"ok": False},
        )
        return dto.ConnectionTestResult(ok=False, status=rec.status, message=msg[:500])

    admin.set_connection_status(principal.tenant_id, connection_id, "tested")
    wf.record_audit(
        principal.tenant_id, actor_id=principal.sub, action="connection.test",
        target_id=connection_id, detail={"ok": True},
    )
    return dto.ConnectionTestResult(ok=True, status="tested")


# 「今すぐ同期」のプロセス内デバウンス。連打で q.sync が無制限に滞留すると、
# 共有 worker の主ループと（実構成では）Drive API quota を1テナントが占有できる
# （レビュー確定major）。ゲートウェイ多重化時はインスタンス毎に上限が付く近似だが、
# 洪水の桁を落とすには十分（毎ポーリングでも同じ検知に到達するため実害が無い）。
_SYNC_DEBOUNCE_SEC = 30.0
_last_sync_enqueue: dict[tuple[str, str], float] = {}


@router.post("/connections/{connection_id}/sync", status_code=202, response_model=dto.ConnectionSyncAccepted)
def sync_connection(
    connection_id: str,
    principal: Principal = Depends(require_role("admin")),
    admin: AdminRepository = Depends(get_admin),
    queue: Queue = Depends(get_queue),
    wf: WorkflowsRepository = Depends(get_workflows),
) -> dto.ConnectionSyncAccepted:
    """「今すぐ同期」（⑤⑥ SaaS連携）。

    フォルダ監視系接続（gdrive/m365/box）の監視フォルダを即時に差分検知する。
    実際の検知・取込は orchestrator-worker 内のポーラー（q.sync 消費）が行う
    （gateway は SaaS に触らない）。定期ポーリングを待たずに取り込みたいときの導線。
    """
    import time as _time

    rec = admin.get_connection(principal.tenant_id, connection_id)
    if rec is None:
        raise ApiError("E1001", "接続が見つかりません", details={"connection_id": connection_id})
    if rec.type not in _FOLDER_SOURCE_TYPES:
        raise ApiError(
            "E1005",
            "今すぐ同期はフォルダ監視系（gdrive/m365/box）のみ対応です",
            details={"type": rec.type},
        )
    if rec.status == "disabled":
        raise ApiError("E1005", "無効化された接続は同期できません", details={"status": rec.status})

    key = (principal.tenant_id, connection_id)
    now = _time.monotonic()
    last = _last_sync_enqueue.get(key)
    if last is not None and now - last < _SYNC_DEBOUNCE_SEC:
        # 直前の要求がまだ有効（結果は同じ）なので新規 enqueue しない
        return dto.ConnectionSyncAccepted(queued=False)
    _last_sync_enqueue[key] = now

    queue.enqueue(
        "q.sync",
        {
            "tenant_id": principal.tenant_id,
            "connection_id": connection_id,
            # worker が種別毎のポーラーへ振り分けるための実種別
            "type": rec.type,
        },
    )
    wf.record_audit(
        principal.tenant_id, actor_id=principal.sub, action="connection.sync",
        target_id=connection_id, detail={"type": rec.type},
    )
    return dto.ConnectionSyncAccepted(queued=True)


@router.post("/webhooks/endpoints", status_code=201, response_model=dto.WebhookEndpointDto)
def add_webhook_endpoint(
    body: dto.WebhookEndpointRequest,
    principal: Principal = Depends(require_role("admin")),
    admin: AdminRepository = Depends(get_admin),
    secret_store: Any = Depends(get_secret_store),
) -> dto.WebhookEndpointDto:
    """Webhook 配信先の登録（§6.2 / §6.4）。

    これが無く、配信先は DB 直投入でしか登録できなかった（export は connections から読む）。
    """
    # 内部ネットワークへ配信させない（SSRF）。export 側の送信直前でも弾いているが、
    # そこで初めて落ちると利用者には「なぜか届かない」としか見えない。登録時に断る。
    if is_blocked_url(body.url):
        raise ApiError("E5001", "配信先 URL が拒否されました", details={"url": body.url})

    # 署名鍵を利用者に選ばせない。弱い鍵を使われると署名検証（§6.4）が意味を失う。
    secret = body.secret or secrets.token_urlsafe(32)
    # P6: 鍵は Secrets Manager に置き、DB には secret_ref（ARN）だけを残す（§16.5）。
    # 保管先が未配線（ローカル）の場合のみ旧方式（config.secret）に fallback
    secret_ref = None
    if secret_store is not None:
        secret_ref = secret_store.create(
            f"{principal.tenant_id}/webhook-{uuid.uuid4().hex[:12]}", secret
        )
    rec = admin.add_webhook_endpoint(
        principal.tenant_id, url=body.url,
        secret=None if secret_ref else secret, name=body.name, secret_ref=secret_ref,
    )
    return dto.WebhookEndpointDto(
        id=rec.id,
        name=rec.name,
        url=body.url,
        status=rec.status,
        created_at=rec.created_at,
        secret=secret,  # ここでしか返さない
    )


@router.get("/webhooks/endpoints", response_model=dto.WebhookEndpointList)
def list_webhook_endpoints(
    principal: Principal = Depends(require_role("admin")),
    admin: AdminRepository = Depends(get_admin),
) -> dto.WebhookEndpointList:
    rows = admin.list_webhook_endpoints(principal.tenant_id)
    return dto.WebhookEndpointList(
        items=[
            dto.WebhookEndpointDto(
                id=r.id,
                name=r.name,
                url=str(r.config.get("url", "")),
                status=r.status,
                created_at=r.created_at,
            )
            for r in rows
        ]
    )


@router.get("/tenants/{tenant_id}/memory", response_model=dto.MemoryList)
def list_memory(
    tenant_id: str,
    doc_type: Optional[str] = None,
    field_name: Optional[str] = None,
    limit: int = 50,
    principal: Principal = Depends(require_role("admin")),
    admin: AdminRepository = Depends(get_admin),
) -> dto.MemoryList:
    """修正メモリ照会（§6.2 / §5.8）。学習された内容を人が確認する唯一の手段。

    パスの tenant_id は自テナントのみ許す。他テナントを指定できると、RLS を
    掻い潜ってデータを引ける入口になる（admin ロールはテナント内の権限であって
    テナントを跨ぐ権限ではない）。
    """
    if tenant_id != principal.tenant_id:
        raise ApiError("E5001", "他テナントのメモリは参照できません", details={"tenant_id": tenant_id})
    rows = admin.list_memories(
        principal.tenant_id, doc_type=doc_type, field_name=field_name, limit=min(limit, 200)
    )
    return dto.MemoryList(items=[dto.MemoryDto(**r.model_dump(exclude={"tenant_id"})) for r in rows])


@router.post("/rules", response_model=dto.RuleDto, status_code=201)
def create_llm_hint(
    body: dto.CreateLlmHintRequest,
    principal: Principal = Depends(require_role("admin")),
    admin: AdminRepository = Depends(get_admin),
) -> dto.RuleDto:
    """LLM最適化ヒント（llm_hint）を人が直接オーサリングする（③）。

    抽出LLMへの自然言語指示。draft で作られ、承認（PATCH active）で有効化される。
    有効化後は memory_lookup が doc_type 単位で拾い、KIE プロンプトの rule_hints に注入される。
    """
    hint = (body.hint_text or "").strip()
    if not hint:
        raise ApiError("E1003", "ヒント本文（hint_text）を入力してください")
    # 上限なしだと1件の巨大ヒントが当該 doc_type の全 KIE プロンプトを恒久的に
    # 肥大化させ、抽出をコンテキスト超過で全件失敗させ得る（レビュー確定）
    if len(hint) > 2000:
        raise ApiError("E1003", "ヒント本文が長すぎます（2000文字以内）")
    doc_type = (body.doc_type or "").strip()
    if not doc_type:
        raise ApiError("E1003", "対象の帳票種別（doc_type）を指定してください")
    # 未登録 doc_type は memory_lookup の等値一致に一度もヒットせず「有効なのに
    # 適用されない」サイレント故障になる（レビュー確定）。作成時に存在を検証する
    if admin.get_schema(principal.tenant_id, doc_type) is None:
        raise ApiError("E1001", "スキーマ未登録の帳票種別です", details={"doc_type": doc_type})
    desc = (body.description or "").strip()
    if len(desc) > 200:
        raise ApiError("E1003", "メモが長すぎます（200文字以内）")
    rule_json: dict[str, Any] = {"hint_text": hint}
    if desc:
        rule_json["description"] = desc
    rec = admin.create_rule(
        RuleRecord(
            id=new_id("rule"),
            tenant_id=principal.tenant_id,
            doc_type=doc_type,
            field_name=(body.field_name or None),
            rule_type="llm_hint",
            rule_json=rule_json,
            status="draft",
            source_correction_ids=[],
            created_by=principal.sub,
        )
    )
    return _rule_dto(rec)


@router.get("/rules", response_model=dto.RuleList)
def list_rules(
    status: Optional[str] = None,
    doc_type: Optional[str] = None,
    principal: Principal = Depends(require_role("admin")),
    admin: AdminRepository = Depends(get_admin),
) -> dto.RuleList:
    rules = admin.list_rules(principal.tenant_id, status=status, doc_type=doc_type)
    return dto.RuleList(items=[_rule_dto(r) for r in rules])


@router.patch("/rules/{rule_id}", response_model=dto.RuleDto)
def patch_rule(
    rule_id: str,
    body: dto.PatchRuleRequest,
    principal: Principal = Depends(require_role("admin")),
    admin: AdminRepository = Depends(get_admin),
) -> dto.RuleDto:
    if body.status not in ("active", "retired"):
        raise ApiError("E1003", "status は active / retired のみ")
    rec = admin.get_rule(principal.tenant_id, rule_id)
    if rec is None:
        raise ApiError("E1001", "ルールが見つかりません", details={"rule_id": rule_id})
    # 有効化は検証合格（再現率≥90%・回帰0件）が条件（§5.8.4）。ただし「人が明示的に
    # 書いた」llm_hint は決定論変換の検証対象ではないため、この条件を課さない。
    # 学習エージェント生成の llm_hint（created_by="agent"）は従来どおりゲート対象
    # （無検証ルールの1クリック注入を防ぐ。レビュー保留所見）。
    human_hint = rec.rule_type == "llm_hint" and rec.created_by != "agent"
    if (
        body.status == "active"
        and not human_hint
        and not is_activatable(rec.validation_report)
    ):
        raise ApiError("E1006", "検証未達のため有効化できません（再現率≥90%・回帰0件が必要）")
    updated = admin.set_rule_status(principal.tenant_id, rule_id, body.status)
    assert updated is not None
    return _rule_dto(updated)


@router.get("/metrics/summary", response_model=dto.MetricsResponse)
def metrics_summary(
    principal: Principal = Depends(require_role("admin")),
    admin: AdminRepository = Depends(get_admin),
) -> dto.MetricsResponse:
    m = admin.metrics_summary(principal.tenant_id)
    return dto.MetricsResponse(**m.model_dump())


# ============ チャットホーム（SCR-01, §3.3/§4.5） ============


@router.post("/chat")
def chat(
    body: dto.ChatRequest,
    principal: Principal = Depends(require_role("viewer")),
    agent: ChatAgent = Depends(get_chat_agent),
) -> StreamingResponse:
    """SSE ストリーム: token / tool_call / confirm_request / done（§3.1/§3.3）。"""

    def _gen() -> Any:
        for ev in agent.stream(principal.tenant_id, body.message):
            yield f"event: {ev.type}\ndata: {json.dumps(ev.data, ensure_ascii=False)}\n\n"

    return StreamingResponse(_gen(), media_type="text/event-stream")


@router.post("/chat/confirm", response_model=dto.ChatConfirmResult)
def chat_confirm(
    body: dto.ChatConfirmRequest,
    principal: Principal = Depends(require_role("admin")),
    admin: AdminRepository = Depends(get_admin),
) -> dto.ChatConfirmResult:
    """confirm_request の承認実行。書込み系ツールは本 API で承認後に実行（§4.5）。"""
    if body.action == "update_schema":
        doc_type = str(body.params.get("doc_type", "invoice"))
        fld = body.params.get("field") or {}
        cur = admin.get_schema(principal.tenant_id, doc_type)
        fields = list(cur.fields) if cur else []
        if any(f.name == fld.get("name") for f in fields):
            return dto.ChatConfirmResult(ok=False, message="同名の項目が既に存在します。")
        fields.append(SchemaFieldDef(**fld))
        rec = admin.put_schema(principal.tenant_id, doc_type, fields)
        return dto.ChatConfirmResult(
            ok=True,
            message=f"スキーマ「{doc_type}」に「{fld.get('label', fld.get('name'))}」を追加し、v{rec.version} として保存しました。",
            detail={"doc_type": rec.doc_type, "version": rec.version},
        )
    raise ApiError("E1003", f"未対応のアクションです: {body.action}")
