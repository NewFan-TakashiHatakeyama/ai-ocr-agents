"""REST エンドポイント（§6.2 / §6.3）。"""

from __future__ import annotations

import json
import logging
import re
import secrets
import uuid
from typing import Any, Optional, TypeVar

from fastapi import APIRouter, Depends, File, Form, Header, Query, Request, Response, UploadFile
from fastapi.responses import StreamingResponse
from newfan_ingest.storage import page_key
from newfan_netguard import is_blocked_url
from newfan_schemas import check_field_name, resolve_regions
from newfan_workflow import WorkflowGraph, build_candidate, catalog, classify_text, has_errors, lint
from newfan_workflow.lint import Finding
from pydantic import BaseModel, ValidationError

from newfan_gateway import dto
from newfan_gateway.auth import Principal, check_min_role
from newfan_gateway.chat import ChatAgent
from newfan_gateway.chat_tools import WRITE_TOOL_MIN_ROLE, ChatTools
from newfan_gateway.config import Settings
from newfan_gateway.conntest import (
    ConnectionTestError,
    build_test_event,
    check_s3,
    check_webhook,
    invalid_url_reason,
)
from newfan_gateway.admin import (
    ACTIVATION_BLOCKED_MESSAGE,
    AdminRepository,
    SchemaArchivedError,
    archived_schema_message,
    can_activate,
)
from newfan_gateway.deps import (
    get_admin,
    get_secret_store,
    get_chat_agent,
    get_chat_tools,
    get_ingestor,
    get_lock_store,
    get_object_store,
    get_orchestrator,
    get_principal,
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
    ConnectionRecord,
    CorrectionRecord,
    DocumentRecord,
    JobRecord,
    PageRecord,
    RuleRecord,
    RunRecord,
    SchemaFieldDef,
    SchemaRecord,
    WorkflowRecord,
    WorkflowRunRecord,
)
from newfan_gateway.repository import DocumentGoneError, Repository
from newfan_ingest import IngestError, UploadInput

router = APIRouter(prefix="/v1")
logger = logging.getLogger(__name__)


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
    # status は繰り返し指定できる（?status=uploaded&status=failed = OR）。
    # 1 値だけの従来の呼び方（web の listDocuments）はそのまま通る。
    status: Optional[list[str]] = Query(default=None),
    doc_type: Optional[str] = None,
    cursor: Optional[str] = None,
    limit: int = 50,
    principal: Principal = Depends(require_role("viewer")),
    repo: Repository = Depends(get_repo),
) -> dto.DocumentList:
    rows, next_cursor = repo.list_documents(
        principal.tenant_id,
        status=None,
        cursor=cursor,
        limit=min(limit, 100),
        doc_type=doc_type,
        statuses=status or None,
    )
    return dto.DocumentList(
        items=[
            dto.DocumentMeta(
                document_id=d.id,
                status=d.status,
                original_name=d.original_name,
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


def _require_usable_schema(admin: AdminRepository, tenant_id: str, schema_id: str) -> SchemaRecord:
    """新しい抽出 run に使える schema_id か（存在し、アーカイブ済みでない）。

    get_schema_by_id はアーカイブ済みの行も返す（過去の run から定義を辿るため）。
    ここで archived を見ないと、一覧から隠しても「再抽出」（document 画面は run.schema_id
    を明示送信する）や API 直叩きでアーカイブ済みの定義に新しい run が積める（C9-D）。
    chat の rerun_extract も同じ判定を持つ（chat_tools.usable_schema_error）。
    """
    rec = admin.get_schema_by_id(tenant_id, schema_id)
    if rec is None:
        raise ApiError("E1001", "スキーマが見つかりません", details={"schema_id": schema_id})
    if rec.archived:
        raise ApiError(
            "E1005",
            archived_schema_message(rec.doc_type),
            details={"schema_id": schema_id, "doc_type": rec.doc_type, "archived": True},
        )
    return rec


def _document_meta(repo: Repository, tenant_id: str, doc: DocumentRecord) -> dto.DocumentMeta:
    """単体取得の DocumentMeta（pages 込み）。

    ページ寸法は**単体取得でのみ**埋める（設計 §6）。一覧 API は DocumentMeta を
    共用しており、そちらで埋めると帳票 1 件ごとに pages を引く N+1 になる。
    """
    pages = repo.get_pages(tenant_id, doc.id)
    return dto.DocumentMeta(
        document_id=doc.id,
        status=doc.status,
        original_name=doc.original_name,
        doc_type=doc.doc_type,
        external_ref=doc.external_ref,
        page_count=doc.page_count,
        pages=[
            dto.PageDim(page_no=p.page_no, width=p.width, height=p.height)
            for p in sorted(pages, key=lambda x: x.page_no)
        ],
    )


@router.get("/documents/{document_id}", response_model=dto.DocumentMeta)
def get_document(
    document_id: str,
    principal: Principal = Depends(require_role("viewer")),
    repo: Repository = Depends(get_repo),
) -> dto.DocumentMeta:
    doc = _require_document(repo, principal.tenant_id, document_id)
    return _document_meta(repo, principal.tenant_id, doc)


@router.patch("/documents/{document_id}", response_model=dto.DocumentMeta)
def patch_document(
    document_id: str,
    body: dto.PatchDocumentRequest,
    principal: Principal = Depends(require_role("admin")),
    repo: Repository = Depends(get_repo),
    admin: AdminRepository = Depends(get_admin),
) -> dto.DocumentMeta:
    """帳票の種別を後から確定する（テンプレート化からの書き戻し）。

    **登録済みスキーマの doc_type しか受け付けない。** classify の declared 分岐は
    `doc.doc_type in latest` の完全一致で、canonical_doc_type（「請求書」→ invoice）を
    通さない。任意文字列を許すと「書き戻したのに declared にならない」が無言で成立する。
    llm_hint の登録が未登録 doc_type を弾いているのと同じ理由。

    権限は admin。唯一の呼び出し元がテンプレート化（PUT /schemas ＝ admin）なので、
    緩めると「テンプレート化はできないのに種別だけ直せる」逆向きの穴になる。

    ロックは取らない。doc_type は原本にも抽出結果にも触らないため、他者が確認中でも
    通す。web 側の発火点は「admin が自分でテンプレート化した直後」だけで競合窓が無い。
    """
    doc_type = body.doc_type.strip()
    if not doc_type:
        raise ApiError("E1003", "帳票種別（doc_type）を指定してください")
    doc = _require_document(repo, principal.tenant_id, document_id)
    if admin.get_schema(principal.tenant_id, doc_type) is None:
        raise ApiError("E1001", "スキーマ未登録の帳票種別です", details={"doc_type": doc_type})
    if doc.doc_type != doc_type:
        repo.set_document_doc_type(principal.tenant_id, document_id, doc_type)
        doc = _require_document(repo, principal.tenant_id, document_id)
    return _document_meta(repo, principal.tenant_id, doc)


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


def _start_extract(
    repo: Repository,
    queue: Queue,
    admin: AdminRepository,
    locks: LockStore,
    principal: Principal,
    document_id: str,
    schema_id: Optional[str],
    options: dto.ExtractOptions,
    supersede_review: bool,
) -> tuple[str, str]:
    """1 帳票の抽出 run を発行して (job_id, run_id) を返す。

    単体の POST /documents/{id}/extract と一括の /documents/extract-batch が共有する
    本体。拒否は ApiError（E1001 不在 / E1005）で、単体はそのまま HTTP エラーに、
    一括は帳票ごとの skipped に翻訳する。E1005 は ``details["reason"]`` で種類を示す
    （confirmed / in_review / locked / processing / active_run）。一括の要約はこれで
    数えるので、文言だけ変えて reason を落とさないこと。冪等キーの扱いは呼び出し側。

    判定の順: 不在 → schema_id → 確定済み → 確定処理中 → 他者ロック → run の競合。
    帳票の状態に関する拒否は supersede_review に**依らず**先に済ませる。
    """
    doc = _require_document(repo, principal.tenant_id, document_id)

    # 空文字の schema_id は「未指定」として扱う。そのまま INSERT すると
    # extraction_runs の FK 違反で 500（E2000 内部エラー）になり、利用者には
    # 原因が一切見えない（API 直叩きで実際に発生）。存在しない ID も 404 で明示する。
    schema_id = (schema_id or "").strip() or None
    if schema_id is not None:
        _require_usable_schema(admin, principal.tenant_id, schema_id)

    # 確定済み（会計連携済みを含む）は supersede_review に関係なく置き換えない
    # （設計 bulk-processing D3 / region-template-editor §3.1）。以前は supersede_review
    # の分岐の中でしか見ておらず、既定（false）の一括投入が has_active_run
    # （processing + needs_review）だけを通って確定済みを queued に落としていた。
    latest = repo.get_latest_run(principal.tenant_id, document_id)
    if latest is not None and latest.status in ("confirmed", "exported"):
        raise ApiError(
            "E1005",
            "確定済みの結果があります。再抽出すると確定値が置き換わります",
            details={"document_id": document_id, "status": latest.status, "reason": "confirmed"},
        )
    # 確定処理中の窓。confirm は documents を in_review にするだけで run は needs_review
    # のまま resume を投げる（get_delete_blocker と同じ理由）。ここで旧 run を superseded
    # に落とすと、resume したワーカーは superseded を confirmed に進めて会計連携まで
    # 流し、その確定値は新 run の後ろに隠れる。
    if doc.status == "in_review":
        raise ApiError(
            "E1005",
            "確定処理中です。完了してから再抽出してください",
            details={"document_id": document_id, "status": doc.status, "reason": "in_review"},
        )
    # 他者のソフトロック（§8.2）。削除と同じく助言的だが、確認中の帳票を横から
    # 置き換えると入力済みの修正は新 run に引き継がれず、相手の確定は E1005 で止まる。
    info = locks.get(principal.tenant_id, document_id)
    if info is not None and info.holder_sub != principal.sub:
        raise ApiError(
            "E1005",
            "他のユーザーが確認中です",
            details={"document_id": document_id, "reason": "locked", "holder": info.holder_name},
        )

    # 再抽出で置き換える旧 run から引き継ぐ options（ワークフローの通知先など）
    inherited_options: dict[str, Any] = {}
    # 既定の競合判定は processing + needs_review（外部連携の二重投入防止）。
    # supersede_review=true のときだけ「今まさに処理中」だけを競合とみなす
    # （chat の rerun_extract と同じ意味論）。テンプレート化直後の再抽出は
    # 「自動発見 run が needs_review」が典型状態で、既定のままでは必ず 409 になる。
    if supersede_review:
        if repo.has_processing_run(principal.tenant_id, document_id):
            raise ApiError(
                "E1005",
                "実行中の Run と競合しています",
                details={"document_id": document_id, "reason": "processing"},
            )
        # ワークフロー起点の run は options に notify 先（hitl_gate を再開させる先）を
        # 持つ。引き継がずに置き換えると、**待機中のワークフローが永久に再開されず、
        # 下流の会計連携が黙って実行されない**。旧 run を終端させる前に控える。
        if latest is not None:
            for key in ("workflow_notify", "workflow_idem"):
                value = (latest.options or {}).get(key)
                if value is not None:
                    inherited_options[key] = value
        # 新 run を作る前に旧 needs_review を終端させる。残すと get_latest_run・
        # 削除ブロッカー・ワークフローの hitl_gate が古い run を見続ける。
        repo.supersede_review_runs(principal.tenant_id, document_id)
    elif repo.has_active_run(principal.tenant_id, document_id):
        raise ApiError(
            "E1005",
            "実行中の Run と競合しています",
            details={"document_id": document_id, "reason": "active_run"},
        )

    run_id = new_id("run")
    job_id = new_id("job")
    repo.create_run(
        RunRecord(
            id=run_id,
            tenant_id=principal.tenant_id,
            document_id=document_id,
            schema_id=schema_id,
            status="processing",
            options={**options.model_dump(), **inherited_options},
        )
    )
    repo.create_job(
        JobRecord(id=job_id, tenant_id=principal.tenant_id, kind="extract", ref_id=run_id)
    )
    repo.set_document_status(principal.tenant_id, document_id, "queued")
    queue.enqueue("q.extract", {"job_id": job_id, "tenant_id": principal.tenant_id, "run_id": run_id})
    return job_id, run_id


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
    locks: LockStore = Depends(get_lock_store),
) -> dto.ExtractAccepted:
    _require_document(repo, principal.tenant_id, document_id)

    cached = _idempotency_hit(request, idempotency_key, principal.tenant_id)
    if cached is not None:
        return dto.ExtractAccepted(**cached)

    job_id, run_id = _start_extract(
        repo,
        queue,
        admin,
        locks,
        principal,
        document_id,
        body.schema_id,
        body.options,
        body.supersede_review,
    )
    payload = {"job_id": job_id, "run_id": run_id}
    _idempotency_store(request, idempotency_key, principal.tenant_id, payload)
    return dto.ExtractAccepted(**payload)


# 一括再抽出の上限。LLM を回す件数の歯止めであり、これを超える母集合は
# 呼び出し側が繰り返す（doc_type 指定なら truncated=true で知らせる）。
EXTRACT_BATCH_MAX = 200
# doc_type 指定の既定の母集合。確定済み（confirmed / exported）は既定で入れない
# （設計 region-template-editor §3.1: 確定値を無警告で置き換えない）。
EXTRACT_BATCH_DEFAULT_STATUSES = ("uploaded", "needs_review", "failed")


@router.post(
    "/documents/extract-batch", status_code=202, response_model=dto.ExtractBatchResponse
)
def extract_batch(
    body: dto.ExtractBatchRequest,
    request: Request,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    principal: Principal = Depends(require_role("uploader")),
    repo: Repository = Depends(get_repo),
    queue: Queue = Depends(get_queue),
    admin: AdminRepository = Depends(get_admin),
    locks: LockStore = Depends(get_lock_store),
) -> dto.ExtractBatchResponse:
    """複数帳票の抽出をまとめて投入する（設計 bulk-processing §2）。

    帳票ごとに単体 /extract と同じ判定（_start_extract）を通し、通らなかった帳票は
    **skipped に理由付きで載せて続行する**。1 件の不在や競合で一括全体を 4xx に
    しない——200 件のうち 1 件が確定済みなだけで残り 199 件が止まるのは使えない。
    応答は常に 202（全件 skipped でも。何が起きたかは本文が伝える）。

    document_ids 指定は一覧の選択をそのまま受けるので、確定済み・確定処理中
    （in_review）・他者ロック中も混ざり得る。それらの拒否は _start_extract が
    supersede_review に依らず行い、skipped の reason で見分けられる。
    """
    tenant_id = principal.tenant_id
    # 単体 /extract と同じキャッシュを使うが、名前空間を分ける。同じキーを単体→一括で
    # 使い回されると応答の形が違って 500 になる。
    idem_key = f"extract-batch:{idempotency_key}" if idempotency_key else None
    cached = _idempotency_hit(request, idem_key, tenant_id)
    if cached is not None:
        return dto.ExtractBatchResponse(**cached)

    has_ids = body.document_ids is not None
    doc_type = (body.doc_type or "").strip() or None
    if has_ids == (doc_type is not None):
        raise ApiError(
            "E1003", "document_ids か doc_type のどちらか一方を指定してください"
        )
    if body.statuses is not None and doc_type is None:
        raise ApiError("E1003", "statuses は doc_type と一緒にだけ指定できます")
    if body.statuses is not None and not body.statuses:
        raise ApiError("E1003", "statuses が空です")
    # 一括全体に効く schema_id は先に検証する。帳票ごとに E1001 で 200 件 skipped に
    # なるより、1 件も触らずに断る方が分かりやすい。
    schema_id = (body.schema_id or "").strip() or None
    if schema_id is not None and admin.get_schema_by_id(tenant_id, schema_id) is None:
        raise ApiError("E1001", "スキーマが見つかりません", details={"schema_id": schema_id})

    truncated = False
    # (document_id, 分かっていれば doc_type)
    targets: list[tuple[str, Optional[str]]]
    if has_ids:
        # 重複は 1 回にする（2 回目は必ず E1005 になり、skipped に紛れ込むだけ）
        ids = list(dict.fromkeys(body.document_ids or []))
        if len(ids) > EXTRACT_BATCH_MAX:
            raise ApiError(
                "E1003",
                f"一度に投入できるのは {EXTRACT_BATCH_MAX} 件までです",
                details={"count": len(ids), "max": EXTRACT_BATCH_MAX},
            )
        targets = [(d, None) for d in ids]
    else:
        statuses = list(body.statuses or EXTRACT_BATCH_DEFAULT_STATUSES)
        rows, next_cursor = repo.list_documents(
            tenant_id,
            status=None,
            cursor=None,
            limit=EXTRACT_BATCH_MAX,
            doc_type=doc_type,
            statuses=statuses,
        )
        truncated = next_cursor is not None
        targets = [(d.id, d.doc_type) for d in rows]

    # doc_type → 最新版 schema_id（無ければ None）。帳票ごとに引き直さない
    latest_by_type: dict[str, Optional[str]] = {}

    def _latest_schema(dt: str) -> Optional[str]:
        if dt not in latest_by_type:
            rec = admin.get_schema(tenant_id, dt)
            latest_by_type[dt] = rec.id if rec is not None else None
        return latest_by_type[dt]

    accepted: list[dto.ExtractBatchAcceptedItem] = []
    skipped: list[dto.ExtractBatchSkippedItem] = []
    for document_id, known_type in targets:
        sid = schema_id
        if sid is None:
            dt = known_type
            if has_ids:
                doc = repo.get_document(tenant_id, document_id)
                if doc is None:
                    # 不在も他テナントも同じ「見つからない」（存在を漏らさない）
                    skipped.append(
                        dto.ExtractBatchSkippedItem(
                            document_id=document_id,
                            code="E1001",
                            message="ドキュメントが見つかりません",
                        )
                    )
                    continue
                dt = doc.doc_type
            if not dt:
                skipped.append(
                    dto.ExtractBatchSkippedItem(
                        document_id=document_id,
                        code="no_schema",
                        message="帳票に種別が無いため、使う定義を決められません",
                    )
                )
                continue
            sid = _latest_schema(dt)
            if sid is None:
                skipped.append(
                    dto.ExtractBatchSkippedItem(
                        document_id=document_id,
                        code="no_schema",
                        message=f"種別「{dt}」の定義（スキーマ）がありません",
                    )
                )
                continue
        try:
            job_id, run_id = _start_extract(
                repo,
                queue,
                admin,
                locks,
                principal,
                document_id,
                sid,
                body.options,
                body.supersede_review,
            )
        except ApiError as exc:
            reason = exc.details.get("reason")
            skipped.append(
                dto.ExtractBatchSkippedItem(
                    document_id=document_id,
                    code=exc.code,
                    message=exc.message,
                    reason=str(reason) if reason is not None else None,
                )
            )
            continue
        accepted.append(
            dto.ExtractBatchAcceptedItem(document_id=document_id, job_id=job_id, run_id=run_id)
        )

    result = dto.ExtractBatchResponse(accepted=accepted, skipped=skipped, truncated=truncated)
    _idempotency_store(request, idem_key, tenant_id, result.model_dump())
    return result


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


@router.get("/documents/{document_id}/spans", response_model=dto.RunSpans)
def get_run_spans(
    document_id: str,
    # 1 始まり。0 や負数を空配列で返すと、UI 側の 0 始まりの取り違えが黙って隠れる
    page: int = Query(default=1, ge=1),
    principal: Principal = Depends(require_role("viewer")),
    repo: Repository = Depends(get_repo),
) -> dto.RunSpans:
    """最新 run の OCR span（除外領域の適用後）をページ単位で返す（設計 D12 / §2.4）。

    テンプレート化画面が、枠を引いた／選んだときに「枠に含まれる文字」を出し、
    例示値（`example_value`）の出どころにする。structure-svc への都度問い合わせは
    採らない（ページあたり数秒〜数十秒）。

    run_spans の行が無い（0008 より前の run、失敗 run、範囲外のページ）場合は
    spans を空で返す。「未抽出」（run が無い）だけをエラーにする。
    """
    _require_document(repo, principal.tenant_id, document_id)
    run = repo.get_latest_run(principal.tenant_id, document_id)
    if run is None:
        raise ApiError("E1001", "抽出結果がありません", details={"document_id": document_id})
    rows = repo.get_run_spans(principal.tenant_id, run.id, page)
    return dto.RunSpans(
        run_id=run.id,
        page_no=page,
        spans=[
            dto.RunSpanDto(span_id=s["span_id"], text=s.get("text") or "", bbox=s.get("bbox"))
            for s in rows
        ],
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
    # 終端済みの run は確定できない。特に superseded（再抽出で置き換えられた旧 run）を
    # 通すと、その checkpoint が resume され **古い抽出結果が confirmed になって会計連携
    # まで流れる**。再抽出を押した直後の画面は新 run に切り替わるまで旧 run を表示して
    # いるので、そこで確定（またはショートカット）を押すと実際に踏む。
    if run.status in ("superseded", "failed"):
        raise ApiError(
            "E1005",
            "この抽出結果は新しい抽出に置き換えられています。最新の結果を読み込んでください"
            if run.status == "superseded"
            else "この抽出は失敗しています。再抽出してください",
            details={"document_id": document_id, "run_id": run.id, "status": run.status},
        )

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
    # 行に出す原本ファイル名。run は名前を持たないので帳票を一括で引く（run ごとに
    # get_document を呼ぶ N+1 にしない）。画面側で一覧 API から名前を引く方式は
    # 一覧の先頭ページ（既定 50 件）に無い帳票だけが ID 表示になり、同じ表の中で
    # 名前と ID が混ざる。
    docs = repo.get_documents_by_ids(principal.tenant_id, [r.document_id for r in runs])
    items = []
    for run in runs:
        pending = int(run.review_summary.get("pending", 0))
        doc = docs.get(run.document_id)
        # 優先度（§8.5 の簡易版）: pending 件数を主指標 + ワークフローの boost
        items.append(
            dto.ReviewQueueItem(
                document_id=run.document_id,
                run_id=run.id,
                pending=pending,
                priority=float(pending + boosts.get(run.document_id or "", 0)),
                original_name=doc.original_name if doc else None,
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
        archived=bool(getattr(rec, "archived", False)),
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
        # 判定は can_activate（PATCH /rules・チャット承認と共通）。llm_hint の検証免除は
        # 「人が明示的に書いた指示」に限る（レビュー保留所見）。
        activatable=can_activate(rec),
    )


@router.get("/doc-types", response_model=dto.DocTypeList)
def list_doc_types(
    principal: Principal = Depends(require_role("viewer")),
    admin: AdminRepository = Depends(get_admin),
) -> dto.DocTypeList:
    """登録済み帳票種別の一覧（取込時の種別指定に使う候補）。

    GET /schemas とは別に置く。アップロードは uploader で通るのに /schemas は
    admin 限定なので、そちらを候補源にすると「アップロードできる人には選択肢が
    見えない」。fields も exclude_regions も返さない——名前と最新版 id だけが要る。
    露出は POST /documents/{id}/classify の candidates（viewer）と同等で、
    新しい情報は増えない。

    スキーマ 0 件のテナントは空配列を返す。エラーにしない（ADR-0006）。
    """
    return dto.DocTypeList(
        items=[
            dto.DocTypeItem(doc_type=s.doc_type, schema_id=s.id, version=s.version)
            for s in admin.list_schemas(principal.tenant_id)
        ]
    )


@router.get("/schemas", response_model=dto.SchemaList)
def list_schemas(
    include_archived: bool = Query(default=False),
    principal: Principal = Depends(require_role("admin")),
    admin: AdminRepository = Depends(get_admin),
) -> dto.SchemaList:
    """doc_type ごとの最新版。アーカイブ済み（C9-D）は既定で出さない。

    抽出のスキーマ選択・ワークフローの extract ノード・テンプレート化はこの一覧を
    候補にするので、隠すだけで「新しく使われる」経路が塞がる。管理画面の
    「アーカイブ済みを表示」だけが include_archived=true で復元候補を引く。
    """
    rows = admin.list_schemas(principal.tenant_id, include_archived=include_archived)
    return dto.SchemaList(items=[_schema_dto(s) for s in rows])


@router.get("/schemas/{doc_type}", response_model=dto.SchemaDto)
def get_schema(
    doc_type: str,
    include_archived: bool = Query(default=False),
    principal: Principal = Depends(require_role("admin")),
    admin: AdminRepository = Depends(get_admin),
) -> dto.SchemaDto:
    rec = admin.get_schema(principal.tenant_id, doc_type)
    if rec is None:
        raise ApiError("E1001", "スキーマが見つかりません", details={"doc_type": doc_type})
    if rec.archived and not include_archived:
        # テンプレート化の編集モードはここを起点にプリロードする。アーカイブ済みを
        # 返すと「編集 → 新版として保存」でアーカイブが黙って解除される側に進む
        raise ApiError(
            "E1001",
            "アーカイブ済みのスキーマです。使うには先に復元してください",
            details={"doc_type": doc_type, "archived": True},
        )
    return _schema_dto(rec)


def _archive_schema(
    doc_type: str, archived: bool, principal: Principal, admin: AdminRepository,
    wf: WorkflowsRepository,
) -> dto.SchemaDto:
    rec = admin.get_schema(principal.tenant_id, doc_type)
    if rec is None:
        raise ApiError("E1001", "スキーマが見つかりません", details={"doc_type": doc_type})
    if rec.archived == archived:
        return _schema_dto(rec)  # 冪等（二重クリック・再送）
    ids = admin.schema_ids_for_doc_type(principal.tenant_id, doc_type)
    if archived:
        # 有効なワークフローの extract.schema_id が（どの版でも）指していれば断る。
        # 隠すと次に有効化し直せなくなる（L009）のに、走っている実行は続くという
        # 中途半端な状態を作らない。draft/paused からの参照は止めない（L009 が断る）
        active = wf.workflows_referencing_schema(principal.tenant_id, ids, statuses=("active",))
        if active:
            raise ApiError(
                "E1005",
                "有効なワークフローがこのスキーマを使っています。先にワークフローを停止してください",
                details={"reason": "workflow_active", "workflows": _workflow_refs(active)},
            )
    updated = admin.set_schema_archived(principal.tenant_id, doc_type, archived)
    if updated is None:  # 取得と更新の間で消えた
        raise ApiError("E1001", "スキーマが見つかりません", details={"doc_type": doc_type})
    wf.record_audit(
        principal.tenant_id, actor_id=principal.sub,
        action="schema.archive" if archived else "schema.unarchive",
        target_id=updated.id, target_type="schema",
        detail={"doc_type": doc_type, "versions": len(ids), "latest_version": updated.version},
    )
    return _schema_dto(updated)


@router.post("/schemas/{doc_type}/archive", response_model=dto.SchemaDto)
def archive_schema(
    doc_type: str,
    principal: Principal = Depends(require_role("admin")),
    admin: AdminRepository = Depends(get_admin),
    wf: WorkflowsRepository = Depends(get_workflows),
) -> dto.SchemaDto:
    """スキーマのアーカイブ（C9-D）。全版の is_active=false。行は消さない。

    extraction_runs.schema_id の FK と過去の抽出結果の定義（項目名・ラベル）を保つ
    ため、削除ではなくアーカイブにする。一覧・doc-types・分類候補・テンプレート化の
    編集から消え、新版の作成（PUT /schemas）は E1005 になる。復元で元に戻る。
    """
    return _archive_schema(doc_type, True, principal, admin, wf)


@router.post("/schemas/{doc_type}/unarchive", response_model=dto.SchemaDto)
def unarchive_schema(
    doc_type: str,
    principal: Principal = Depends(require_role("admin")),
    admin: AdminRepository = Depends(get_admin),
    wf: WorkflowsRepository = Depends(get_workflows),
) -> dto.SchemaDto:
    return _archive_schema(doc_type, False, principal, admin, wf)


@router.put("/schemas", response_model=dto.SchemaDto)
def put_schema(
    body: dto.PutSchemaRequest,
    principal: Principal = Depends(require_role("admin")),
    admin: AdminRepository = Depends(get_admin),
) -> dto.SchemaDto:
    existing = admin.get_schema(principal.tenant_id, body.doc_type)
    if existing is not None and existing.archived:
        # アーカイブ済み（C9-D）へは新規作成モードでも編集でも新版を足さない。足すと
        # その版だけ is_active=true になり、一覧に戻る＝アーカイブが黙って解除される。
        # 一覧から消えている以上「同名を新規作成」は起こり得る操作なので、復元を案内する
        raise ApiError(
            "E1005",
            f"スキーマ「{body.doc_type}」はアーカイブ済みです。"
            "使うには先に復元してください（アーカイブ済みを表示 → 復元）",
            details={"doc_type": body.doc_type, "archived": True},
        )
    # 新規作成モードはサーバ側で重複を拒否する。クライアントの重複チェックは一覧が
    # 陳腐化していると素通りし、既存スキーマを黙って新版で置換してしまう（レビュー確定）。
    if body.create and existing is not None:
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
        # 予約名（__pages__ / __region__ / 先頭 __）は集約 ReviewItem の擬似 field 名と
        # 衝突する（設計 region-field-add-and-hint-v2 D9）。SchemaFieldDef の validator
        # でも落ちるが、そこで落とすと pydantic の ValidationError が未捕捉 500 になる。
        # DTO（SchemaFieldDto）に validator を置くと FastAPI の 422 形式で返ってしまい
        # プロジェクトのエラー封筒（E1003）にならないので、ここで明示的に検査する。
        try:
            check_field_name(f.name)
        except ValueError as exc:
            raise ApiError("E1003", str(exc), details={"field": f.name}) from exc
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
    try:
        rec = admin.put_schema(  # 常に新版
            principal.tenant_id,
            body.doc_type,
            fields,
            exclude_regions=body.exclude_regions,
            source_page_count=body.source_page_count,
        )
    except SchemaArchivedError as exc:
        # 上の事前チェックとは別トランザクション。その間にアーカイブされた場合
        raise ApiError(
            "E1005", str(exc), details={"doc_type": body.doc_type, "archived": True}
        ) from exc
    return _schema_dto(rec)


# ---------- ワークフロー実行（§16 設計 v0.2 §11 / P3） ----------

WORKFLOW_STREAM = "q.workflow"


def _run_document_deleted(rec: WorkflowRunRecord) -> bool:
    """帳票の削除で切り離された run か（db.delete_document が state に立てる旗）。"""
    return bool((rec.state or {}).get("document_deleted"))


def _run_summary(rec: WorkflowRunRecord) -> dto.WorkflowRunSummaryDto:
    trigger = rec.trigger or {}
    return dto.WorkflowRunSummaryDto(
        id=rec.id,
        workflow_id=rec.workflow_id,
        workflow_version=rec.workflow_version,
        document_id=rec.document_id,
        status=rec.status,
        error=rec.error,
        started_at=rec.started_at,
        finished_at=rec.finished_at,
        trigger_type=trigger.get("type"),
        trigger_node_id=trigger.get("node_id"),
        document_deleted=_run_document_deleted(rec),
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
    if _run_document_deleted(rec):
        # 帳票の削除は waiting_hitl / running の run を failed に終端化して切り離す
        # （db.delete_document）。それを retry すると checkpoint から interrupt が
        # 再び立ち、帳票の無い run が waiting_hitl に蘇る（確定する画面が無く、
        # 止める API も無いので永久に残る）。document_id の NULL だけでは判別しない:
        # schedule 発火の run は最初から帳票を持たず、それは retry してよい
        raise ApiError(
            "E1005",
            "帳票が削除された run は retry できません",
            details={"status": rec.status, "document_deleted": True},
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


def _schema_usable(admin: AdminRepository, tenant_id: str, schema_id: str) -> bool:
    """L009 のうち「アーカイブ済み」の側（C9-D）。存在判定は wf.schema_exists が担う。

    ここ（ルータ）に置くのは、InMemory の WorkflowsRepository が版も is_active も
    持たないため。admin repo（InMemory / Pg 両方）が archived を知っている。
    """
    rec = admin.get_schema_by_id(tenant_id, schema_id)
    return rec is None or not rec.archived


def _lint_workflow(
    rec: "WorkflowRecord",
    graph: "WorkflowGraph",
    wf: WorkflowsRepository,
    tenant_id: str,
    admin: AdminRepository,
) -> tuple[list[Finding], list[str]]:
    findings = lint(
        graph,
        auto_confirm=rec.auto_confirm,
        schema_exists=lambda sid: (
            wf.schema_exists(tenant_id, sid) and _schema_usable(admin, tenant_id, sid)
        ),
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


@router.delete("/workflows/{workflow_id}", response_model=dto.WorkflowDeleted)
def delete_workflow(
    workflow_id: str,
    principal: Principal = Depends(require_role("admin")),
    wf: WorkflowsRepository = Depends(get_workflows),
) -> dto.WorkflowDeleted:
    """ワークフローの削除（C9-D）。**active でなく、run が 1 件も無い**ものだけ。

    run が残る定義を消すと、履歴（workflow_runs / node_runs）の参照先が無くなり
    retry も再現もできない。使わなくなったものは停止（pause）のまま残す。
    条件は Pg 実装の DELETE 文にも含める（事前チェックとは別トランザクション）。
    """
    rec = wf.get_workflow(principal.tenant_id, workflow_id)
    if rec is None:
        raise ApiError("E1001", "ワークフローが見つかりません", details={"workflow_id": workflow_id})
    if rec.status == "active":
        raise ApiError(
            "E1005",
            "有効なワークフローは削除できません。先に停止してください",
            details={"reason": "active", "status": rec.status},
        )
    if wf.has_runs(principal.tenant_id, workflow_id):
        raise ApiError(
            "E1005",
            "実行履歴があるワークフローは削除できません。停止のまま残してください",
            details={"reason": "has_runs", "status": rec.status},
        )
    ok = wf.delete_workflow(
        principal.tenant_id, workflow_id, actor_id=principal.sub, detail={"by": "api"}
    )
    if not ok:
        # 事前チェックの後に有効化・実行された（別トランザクション）か、同時削除
        if wf.get_workflow(principal.tenant_id, workflow_id) is None:
            raise ApiError(
                "E1001", "ワークフローが見つかりません", details={"workflow_id": workflow_id}
            )
        raise ApiError(
            "E1005",
            "有効化または実行されたため削除できません。停止のまま残してください",
            details={"reason": "changed"},
        )
    return dto.WorkflowDeleted(workflow_id=workflow_id)


@router.post("/workflows/{workflow_id}/lint", response_model=dto.WorkflowLintResponse)
def lint_workflow(
    workflow_id: str,
    body: Optional[dto.WorkflowLintRequest] = None,
    principal: Principal = Depends(require_role("admin")),
    wf: WorkflowsRepository = Depends(get_workflows),
    admin: AdminRepository = Depends(get_admin),
) -> dto.WorkflowLintResponse:
    rec = wf.get_workflow(principal.tenant_id, workflow_id)
    if rec is None:
        raise ApiError("E1001", "ワークフローが見つかりません", details={"workflow_id": workflow_id})
    graph_data = body.graph_json if (body and body.graph_json is not None) else rec.graph_json
    graph = _validate_graph(graph_data)
    findings, unsupported = _lint_workflow(rec, graph, wf, principal.tenant_id, admin)
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

    findings, unsupported = _lint_workflow(rec, graph, wf, principal.tenant_id, admin)
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
# gateway に疎通テスト経路（POST /connections/{id}/test → tested）がある型。これらは
# 再有効化（PATCH status=active）で疎通確認済み（active）に格上げしない（untested に戻す）
_TESTABLE_CONNECTION_TYPES = {"postgres"}
# secret_ref の秘密を gateway 自身が作る型（add_webhook_endpoint の署名鍵）。接続の削除で
# 一緒に消す。それ以外の型の secret_ref は利用者が登録した秘密で、gateway は触らない
_GATEWAY_OWNED_SECRET_TYPES = {"webhook"}
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


def _connection_dto(r: Any) -> dto.ConnectionDto:
    return dto.ConnectionDto(
        id=r.id, type=r.type, name=r.name,
        # 旧 webhook 行の config.secret（平文）を API に出さない（再帰マスク）
        config=_redact_config(r.config or {}),
        secret_ref=r.secret_ref, allowed_tables=r.allowed_tables,
        status=r.status, created_at=r.created_at,
        last_synced_at=r.last_synced_at,
        last_sync_status=r.last_sync_status,
        last_sync_error=r.last_sync_error,
    )


def _workflow_refs(rows: list[Any]) -> list[dict[str, Any]]:
    """E1005 の details に載せる「参照しているワークフロー」。UI が名前で示せる分だけ。"""
    return [{"id": w.id, "name": w.name, "status": w.status, "version": w.version} for w in rows]


@router.get("/connections", response_model=dto.ConnectionList)
def list_connections(
    principal: Principal = Depends(require_role("admin")),
    admin: AdminRepository = Depends(get_admin),
) -> dto.ConnectionList:
    rows = admin.list_connections(principal.tenant_id)
    return dto.ConnectionList(items=[_connection_dto(r) for r in rows])


@router.patch("/connections/{connection_id}", response_model=dto.ConnectionDto)
def patch_connection_status(
    connection_id: str,
    body: dto.ConnectionStatusRequest,
    principal: Principal = Depends(require_role("admin")),
    admin: AdminRepository = Depends(get_admin),
    wf: WorkflowsRepository = Depends(get_workflows),
) -> dto.ConnectionDto:
    """接続の無効化 / 再有効化（C9-D）。

    disabled にすると sink・トリガー・「今すぐ同期」のいずれも使わなくなる
    （orchestrator は status IN ('active','tested') / <> 'disabled' で引く）。
    **有効（active）なワークフローが使っている接続は無効化しない。** 黙って止めると
    そのワークフローの実行が「接続が無い」で失敗し始め、利用者は接続画面の操作と
    結び付けられない。先にワークフローを停止させる（E1005 に一覧を載せる）。
    draft/paused からの参照は止めない——有効化時に L010 が「疎通未確認/無効」で断る。

    **再有効化の着地点は型で違う。** L010（connection_ok）・dry-run・orchestrator の
    sink は active/tested を「疎通確認済み」として扱う。postgres は疎通テスト
    （POST /connections/{id}/test）で tested になる型なので、disabled → active を
    素通しすると、一度もテストしていない接続（untested → 無効化 → 再有効化。UI の
    通常操作で起きる）が疎通確認済みに格上げされ、初回の実行が接続エラーで落ちるか、
    検証されていない DSN へ書き込む。無効化前の状態は持っていない（列が無い）ので、
    postgres の再有効化は常に untested に戻し、テストを踏ませる（SELECT 1 だけ）。
    untested/tested のまま active を求められても格上げしない（既に有効なので no-op）。
    webhook / s3 / フォルダ監視系は gateway にテスト経路が無く、active が有効化そのもの。
    """
    rec = admin.get_connection(principal.tenant_id, connection_id)
    if rec is None:
        raise ApiError("E1001", "接続が見つかりません", details={"connection_id": connection_id})
    target: str = body.status
    if body.status == "active" and rec.type in _TESTABLE_CONNECTION_TYPES:
        if rec.status != "disabled":
            return _connection_dto(rec)  # untested/tested は既に有効。tested を巻き戻さない
        target = "untested"
    if rec.status == target:
        return _connection_dto(rec)
    if body.status == "disabled":
        active = wf.workflows_referencing_connection(
            principal.tenant_id, connection_id, statuses=("active",)
        )
        if active:
            raise ApiError(
                "E1005",
                "有効なワークフローがこの接続を使っています。先にワークフローを停止してください",
                details={"reason": "workflow_active", "workflows": _workflow_refs(active)},
            )
    updated = admin.set_connection_status(principal.tenant_id, connection_id, target)
    if updated is None:  # 取得と更新の間で消えた
        raise ApiError("E1001", "接続が見つかりません", details={"connection_id": connection_id})
    wf.record_audit(
        principal.tenant_id, actor_id=principal.sub,
        action="connection.disable" if body.status == "disabled" else "connection.enable",
        target_id=connection_id, target_type="connection",
        detail={"type": rec.type, "from": rec.status, "to": target},
    )
    return _connection_dto(updated)


@router.delete("/connections/{connection_id}", response_model=dto.ConnectionDeleted)
def delete_connection(
    connection_id: str,
    principal: Principal = Depends(require_role("admin")),
    admin: AdminRepository = Depends(get_admin),
    wf: WorkflowsRepository = Depends(get_workflows),
    secret_store: Any = Depends(get_secret_store),
) -> dto.ConnectionDeleted:
    """接続の削除（C9-D）。**どのワークフロー版からも参照されていない接続だけ**消せる。

    「版」には現在の定義（status を問わない）と、run が持つスナップショット
    （§11.1 版固定。retry の再開先・履歴の再現に要る）の両方を含める。参照が
    残っている接続は削除ではなく無効化（PATCH status=disabled）で止める。

    **gateway が作った秘密は一緒に消す。** webhook の署名鍵は add_webhook_endpoint が
    Secrets Manager に置き、DB には secret_ref しか無い。行だけ消すと、鍵が生きたまま
    参照する物が無くなり（保管料も掛かり続け）、監査にも残らないので突き合わせが
    できない。行を消した後に秘密を消し（逆順だと、参照の競合で行が消せなかったときに
    生きている接続の鍵を壊す）、結果を監査（secret_ref / secret_deleted）に残す。
    postgres の secret_ref は利用者が登録した秘密なので触らない。
    """
    rec = admin.get_connection(principal.tenant_id, connection_id)
    if rec is None:
        raise ApiError("E1001", "接続が見つかりません", details={"connection_id": connection_id})
    refs = wf.workflows_referencing_connection(principal.tenant_id, connection_id)
    run_refs = wf.runs_referencing_connection(principal.tenant_id, connection_id)
    if refs or run_refs:
        raise ApiError(
            "E1005",
            "ワークフローから参照されている接続は削除できません。"
            "使わなくするには無効化してください",
            details={
                "reason": "referenced",
                "workflows": _workflow_refs(refs),
                "run_count": run_refs,
            },
        )
    counts = admin.delete_connection(principal.tenant_id, connection_id)
    if counts is None:
        # 事前チェックの後に参照が付いた（別トランザクション）。Pg 実装は DELETE 文で
        # 再検査するので、ここに来るのは「参照あり」か「同時削除で先を越された」
        if admin.get_connection(principal.tenant_id, connection_id) is None:
            raise ApiError(
                "E1001", "接続が見つかりません", details={"connection_id": connection_id}
            )
        raise ApiError(
            "E1005",
            "ワークフローから参照されている接続は削除できません。"
            "使わなくするには無効化してください",
            details={"reason": "referenced"},
        )
    # gateway 所有の秘密（webhook の署名鍵）だけ消す。失敗しても行の削除は戻さない
    # （既に消えている）。secret_deleted=false と secret_ref を監査に残し、後から
    # Secrets Manager 側を突き合わせられるようにする
    secret_deleted: Optional[bool] = None
    if rec.type in _GATEWAY_OWNED_SECRET_TYPES and rec.secret_ref:
        secret_deleted = False
        if secret_store is not None:
            try:
                secret_store.delete(rec.secret_ref)
                secret_deleted = True
            except Exception:  # noqa: BLE001 - 行は消えている。監査に残して返す
                logger.exception(
                    "接続 %s の秘密 %s を削除できませんでした", connection_id, rec.secret_ref
                )
    wf.record_audit(
        principal.tenant_id, actor_id=principal.sub, action="connection.delete",
        target_id=connection_id, target_type="connection",
        detail={
            "type": rec.type, "name": rec.name, "status": rec.status,
            "secret_ref": rec.secret_ref, "secret_deleted": secret_deleted, **counts,
        },
    )
    return dto.ConnectionDeleted(
        connection_id=connection_id,
        cursors_deleted=counts.get("cursors_deleted", 0),
        secret_deleted=secret_deleted,
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
    # webhook の config.url は /webhooks/endpoints と同じ二段の一段目をここで見る:
    # 内部ネットワーク宛て（SSRF）と httpx が受け付けない形（「:abc」のようなポート・
    # 改行入り）は登録時に断る。後者は is_blocked_url（urllib.parse）が通してしまい、
    # 疎通テストで初めて httpx.InvalidURL（HTTPError ではない）が出て 500 になっていた。
    # url 自体の有無は従来どおり疎通テストで「config.url が未設定」と返す
    if body.type == "webhook":
        hook_url = str(body.config.get("url") or "")
        if hook_url:
            bad = invalid_url_reason(hook_url)
            if bad is not None:
                raise ApiError("E4001", bad, details={"url": hook_url})
            if is_blocked_url(hook_url):
                raise ApiError("E5001", "配信先 URL が拒否されました", details={"url": hook_url})
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
    """疎通テスト（§16.5 / P6）。成功で status='tested' になる。

    sink/トリガーは tested/active の接続しか使わないため、これを通すまで実行に乗らない
    （lint L010）。種別ごとの実体:
    - postgres: SELECT 1（失敗は従来どおり 200 + ok=false で理由を返す）
    - webhook: 本配信と同じ署名・ヘッダで {"event":"test", "text": …} を 1 回送る
      （text は Slack incoming webhook 互換の通知先 = sink.notify 向け。無いと 400
      no_text で断られ tested になれない）。SSRF ガードを通し、2xx で成功。失敗
      （非 2xx・ネットワーク・URL 拒否／不正）は 422 で理由を返す
    - s3: sink と同じ boto3.client("s3")（ただし 5 秒・再試行なし）で HeadBucket。
      失敗は 422 で理由を返す
    - フォルダ監視系（gdrive/m365/box）は「今すぐ同期」が疎通テストを兼ねる
      （同期成功で worker が tested に上げる）

    status='disabled' は運用側が API 外で止めた印（同期も断る）。成功で無条件に
    tested に書き戻すと、テナント管理者の 1 クリックで配信が再開してしまうため断る。
    無効化した接続で成功を tested にすると、再有効化を踏まずに無効化が黙って解けて
    しまう。再有効化（PATCH status=active → untested）してからテストする（C9-D）。
    """
    rec = admin.get_connection(principal.tenant_id, connection_id)
    if rec is None:
        raise ApiError("E1001", "接続が見つかりません", details={"connection_id": connection_id})
    if rec.status == "disabled":
        raise ApiError(
            "E1005",
            "無効化された接続は疎通テストできません。先に再有効化してください",
            details={"status": rec.status},
        )
    if rec.type in ("webhook", "s3"):
        try:
            if rec.type == "webhook":
                _ping_webhook_connection(rec, secret_store)
            else:
                _ping_s3_connection(rec)
        except ConnectionTestError as exc:
            wf.record_audit(
                principal.tenant_id, actor_id=principal.sub, action="connection.test",
                target_id=connection_id, detail={"ok": False, "reason": exc.message},
            )
            raise ApiError(
                "E4001", exc.message, details={"type": rec.type, **exc.details}
            ) from exc
        return _mark_connection_tested(admin, wf, principal, connection_id)
    if rec.type != "postgres":  # 対応型を増やすときは _TESTABLE_CONNECTION_TYPES も更新する
        raise ApiError(
            "E1005",
            "疎通テストは postgres / webhook / s3 のみ対応です"
            "（フォルダ監視系は「今すぐ同期」が疎通テストを兼ねます）",
            details={"type": rec.type, "supported": ["postgres", "webhook", "s3"]},
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

    return _mark_connection_tested(admin, wf, principal, connection_id)


def _mark_connection_tested(
    admin: AdminRepository, wf: WorkflowsRepository, principal: Principal, connection_id: str
) -> dto.ConnectionTestResult:
    """疎通成功の共通末尾: status='tested'（connection_ok / L010 が要求する状態）+ 監査。"""
    admin.set_connection_status(principal.tenant_id, connection_id, "tested")
    wf.record_audit(
        principal.tenant_id, actor_id=principal.sub, action="connection.test",
        target_id=connection_id, detail={"ok": True},
    )
    return dto.ConnectionTestResult(ok=True, status="tested")


def _ping_webhook_connection(rec: ConnectionRecord, secret_store: Any) -> None:
    """webhook 接続へ署名付き test イベントを 1 回送る（失敗は ConnectionTestError）。

    署名鍵は本配信（orchestrator の get_webhook_connection）と同じ優先順で解決する:
    secret_ref（Secrets Manager）→ 旧行の config.secret。
    """
    from datetime import datetime, timezone

    url = str(rec.config.get("url") or "")
    if not url:
        raise ConnectionTestError("config.url（配信先 URL）が未設定です")
    if rec.secret_ref:
        if secret_store is None:
            raise ConnectionTestError("secret_ref があるのに秘密の保管先が未配線です")
        try:
            secret = str(secret_store.get(rec.secret_ref))
        except Exception as exc:  # noqa: BLE001 - 保管先の例外文言（ARN 等）を外に出さない
            raise ConnectionTestError(
                "secret_ref の秘密を取得できません（Secrets Manager の名前・権限を確認）",
                details={"secret_ref": rec.secret_ref},
            ) from exc
    else:
        secret = str(rec.config.get("secret") or "")
    event = build_test_event(
        rec.id, tenant_id=rec.tenant_id,
        occurred_at=datetime.now(timezone.utc).isoformat(),
    )
    check_webhook(url, secret, event)


def _ping_s3_connection(rec: ConnectionRecord) -> None:
    bucket = str(rec.config.get("bucket") or "")
    if not bucket:
        raise ConnectionTestError("config.bucket（バケット名）が未設定です")
    check_s3(bucket)


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
    # 有効化は検証合格（再現率≥90%・回帰0件）が条件（§5.8.4）。判定はチャット承認
    # （manage_rules）と共通の can_activate（人が書いた llm_hint だけ免除）。
    if body.status == "active" and not can_activate(rec):
        raise ApiError("E1006", ACTIVATION_BLOCKED_MESSAGE)
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


_ParamsT = TypeVar("_ParamsT", bound=BaseModel)


def _confirm_params(model: type[_ParamsT], params: dict[str, Any]) -> _ParamsT:
    """confirm_request 由来の params を action ごとの DTO で検証する（不正は E1003）。

    LLM が組んだ引数を UI がそのまま返してくるので、欠落や語彙違いは起こり得る。
    未捕捉 500 ではなく、どのキーが悪いかを details で返す。
    """
    try:
        return model.model_validate(params)
    except ValidationError as exc:
        raise ApiError(
            "E1003",
            "承認内容（params）が不正です",
            details={
                "errors": [
                    {"loc": ".".join(str(p) for p in e.get("loc", ())), "msg": str(e.get("msg", ""))}
                    for e in exc.errors(include_url=False)
                ]
            },
        ) from exc


@router.post("/chat/confirm", response_model=dto.ChatConfirmResult)
def chat_confirm(
    body: dto.ChatConfirmRequest,
    request: Request,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    principal: Principal = Depends(get_principal),
    tools: ChatTools = Depends(get_chat_tools),
) -> dto.ChatConfirmResult:
    """confirm_request の承認実行。書込み系ツールは本 API で承認後に実行（§4.5）。

    supervisor が提案した書込みツール（update_schema / rerun_extract / manage_rules）を
    **同じ ChatTools** で実行する。必要ロールは同じ操作の非チャット API と揃える
    （WRITE_TOOL_MIN_ROLE: rerun_extract=uploader、他は admin）。以前は update_schema しか
    受けず、rerun_extract / manage_rules の承認カードは必ず E1003 で失敗していた。

    Idempotency-Key は POST /documents/{id}/extract と同じ扱い（同キーはキャッシュ応答）。
    承認カードの連打・再送で rerun_extract が二重に Run を発行したり、2 回目の
    ok=False（現在処理中 / 同名の項目が既に存在）が 1 回目の成功を UI 上で上書きしたり
    しないための保険。UI はカード生成時に 1 つ鍵を作り、そのカードの承認では使い回す。
    """
    min_role = WRITE_TOOL_MIN_ROLE.get(body.action)
    if min_role is None:
        raise ApiError("E1003", f"未対応のアクションです: {body.action}")
    check_min_role(principal, min_role)
    tenant_id = principal.tenant_id

    cached = _idempotency_hit(request, idempotency_key, tenant_id)
    if cached is not None:
        return dto.ChatConfirmResult(**cached)
    result = _run_chat_confirm(body, tenant_id, tools)
    _idempotency_store(request, idempotency_key, tenant_id, result.model_dump())
    return result


def _run_chat_confirm(
    body: dto.ChatConfirmRequest, tenant_id: str, tools: ChatTools
) -> dto.ChatConfirmResult:
    """action ごとに params を検証して ChatTools を実行する（権限・冪等鍵は呼び出し側）。"""
    if body.action == "update_schema":
        us = _confirm_params(dto.ChatUpdateSchemaParams, body.params)
        res = tools.update_schema(tenant_id, us.doc_type, us.field)
        if not res["ok"]:
            return dto.ChatConfirmResult(ok=False, message=res["message"])
        label = us.field.get("label", us.field.get("name"))
        return dto.ChatConfirmResult(
            ok=True,
            message=f"スキーマ「{us.doc_type}」に「{label}」を追加し、v{res['version']} として保存しました。",
            detail={"doc_type": res["doc_type"], "version": res["version"]},
        )

    if body.action == "rerun_extract":
        re_ = _confirm_params(dto.ChatRerunExtractParams, body.params)
        res = tools.rerun_extract(
            tenant_id,
            re_.document_id,
            schema_id=re_.schema_id,
            supersede_review=re_.supersede_review,
        )
        if not res["ok"]:
            return dto.ChatConfirmResult(
                ok=False, message=res["message"], detail={"document_id": re_.document_id}
            )
        return dto.ChatConfirmResult(
            ok=True,
            message=f"再抽出を開始しました（{re_.document_id}）。完了までしばらくお待ちください。",
            detail={"document_id": re_.document_id, "job_id": res["job_id"], "run_id": res["run_id"]},
        )

    mr = _confirm_params(dto.ChatManageRulesParams, body.params)
    res = tools.manage_rules(tenant_id, mr.rule_id, mr.status)
    if not res["ok"]:
        return dto.ChatConfirmResult(ok=False, message=res["message"], detail={"rule_id": mr.rule_id})
    verb = "有効化" if mr.status == "active" else "退役"
    return dto.ChatConfirmResult(
        ok=True,
        message=f"ルール {mr.rule_id} を{verb}しました。",
        detail={"rule_id": res["rule_id"], "status": res["status"]},
    )
