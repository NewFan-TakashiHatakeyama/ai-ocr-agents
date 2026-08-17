"""API リクエスト/レスポンス DTO（§6.3）。"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from newfan_schemas import ExtractedField, TableResult


class DocumentCreated(BaseModel):
    document_id: str
    page_count: Optional[int]
    status: str


class DocumentMeta(BaseModel):
    document_id: str
    status: str
    doc_type: Optional[str] = None
    external_ref: Optional[str] = None
    page_count: Optional[int] = None


class DocumentList(BaseModel):
    items: list[DocumentMeta]
    next_cursor: Optional[str] = None


class ExtractOptions(BaseModel):
    force_vl: bool = False
    two_model_check: bool = False
    language: str = "ja"


class ExtractRequest(BaseModel):
    schema_id: Optional[str] = None
    options: ExtractOptions = Field(default_factory=ExtractOptions)


class ExtractAccepted(BaseModel):
    job_id: str
    run_id: str


class JobStatus(BaseModel):
    job_id: str
    kind: str
    status: str
    error_code: Optional[str] = None


class ResultResponse(BaseModel):
    document_id: str
    run_id: str
    status: str
    result_version: int
    engine_versions: dict[str, Any]
    fields: list[ExtractedField]
    tables: list[TableResult]
    review_summary: dict[str, Any]
    fallback_pages: list[int] = Field(default_factory=list)  # VL フォールバックしたページ（§5.4）


class CorrectionItem(BaseModel):
    field_name: str
    original_value: Optional[str] = None
    corrected_value: str
    # 学習用（§5.8）。supplier_key は発行者名の正規化キー、context は周辺原文（embedding 入力）。
    supplier_key: Optional[str] = None
    context: Optional[str] = None
    # レビュアのメモ。専用の列は無く、context 未指定時の embedding 入力として使う。
    note: Optional[str] = None


class CorrectionsRequest(BaseModel):
    run_id: str
    items: list[CorrectionItem]
    version: int


class CorrectionsAccepted(BaseModel):
    correction_ids: list[str]


class ConfirmRequest(BaseModel):
    run_id: Optional[str] = None
    overrides: Optional[dict[str, Any]] = None


class ConfirmAccepted(BaseModel):
    status: str = "accepted"


class ReviewQueueItem(BaseModel):
    document_id: str
    run_id: str
    pending: int
    priority: float


class ReviewQueue(BaseModel):
    items: list[ReviewQueueItem]


class SignedUrl(BaseModel):
    url: str
    expires_in: int


class LockStatus(BaseModel):
    """検証画面ソフトロックの状態（§8.2）。"""

    document_id: str
    locked: bool  # 現在ロックされているか
    held_by_me: bool  # 呼び出し元が保持者か（True の間だけ編集可）
    holder: Optional[str] = None  # 保持者の表示名（他者ロック時のバナー表示用）
    remaining_sec: int = 0  # 期限までの残り秒
    ttl_sec: int = 0  # 取得時の TTL


# ---- 管理画面 DTO（SCR-04/05/06） ----


class SchemaFieldDto(BaseModel):
    name: str
    label: Optional[str] = None
    type: str = "string"
    required: bool = False
    critical: bool = False
    columns: Optional[list[dict[str, Any]]] = None


class SchemaDto(BaseModel):
    # id（field_schemas.id）は抽出やワークフローの schema_id に使う。UI がスキーマを
    # 選ばせるには id が要る（従来は doc_type しか返しておらず ID を手転記していた）。
    id: str
    doc_type: str
    version: int
    fields: list[SchemaFieldDto]


class SchemaList(BaseModel):
    items: list[SchemaDto]


class PutSchemaRequest(BaseModel):
    doc_type: str
    fields: list[SchemaFieldDto]


class RuleDto(BaseModel):
    id: str
    doc_type: Optional[str] = None
    supplier_key: Optional[str] = None
    field_name: Optional[str] = None
    rule_type: str
    rule_json: dict[str, Any]
    status: str
    validation_report: Optional[dict[str, Any]] = None
    source_correction_ids: list[str] = Field(default_factory=list)
    created_by: str = "agent"
    activatable: bool = False


class RuleList(BaseModel):
    items: list[RuleDto]


class MemoryDto(BaseModel):
    """修正メモリ 1 件（§5.8 / DD-06）。何をどう直した結果が学習されたかを見せる。"""

    id: str
    correction_log_id: str
    embed_model: str
    field_name: str
    original_value: Optional[str] = None
    corrected_value: str = ""
    doc_type: Optional[str] = None
    supplier_key: Optional[str] = None
    context: Optional[str] = None
    document_id: Optional[str] = None
    created_at: Optional[str] = None


class MemoryList(BaseModel):
    items: list[MemoryDto]


class WorkflowCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    graph_json: dict[str, Any]
    auto_confirm: bool = False


class WorkflowUpdateRequest(BaseModel):
    """PUT = 新版作成（version+1・draft に戻る）。graph_json は必須。"""

    graph_json: dict[str, Any]
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    auto_confirm: Optional[bool] = None


class WorkflowSummaryDto(BaseModel):
    id: str
    name: str
    status: str
    version: int
    auto_confirm: bool
    updated_at: Optional[str] = None


class WorkflowDto(WorkflowSummaryDto):
    graph_json: dict[str, Any]


class WorkflowList(BaseModel):
    items: list[WorkflowSummaryDto]


class LintFindingDto(BaseModel):
    rule: str
    severity: str
    message: str
    node_id: Optional[str] = None


class WorkflowLintRequest(BaseModel):
    """省略時は保存済みの graph を lint する。graph_json を渡すと保存せずに検査だけ行う
    （エディタの編集中プレビュー用）。"""

    graph_json: Optional[dict[str, Any]] = None


class WorkflowLintResponse(BaseModel):
    findings: list[LintFindingDto]
    # activate 可能か（error なし かつ 未実装ノードなし）。ボタン活性の判定に使う
    activatable: bool
    unsupported_types: list[str] = Field(default_factory=list)


class WorkflowRunRequest(BaseModel):
    document_id: str = Field(min_length=1)


class WorkflowRunAccepted(BaseModel):
    workflow_run_id: str
    workflow_version: int


class WorkflowNodeRunDto(BaseModel):
    node_id: str
    node_type: str
    status: str
    attempt: int
    output: Optional[dict[str, Any]] = None
    error: Optional[dict[str, Any]] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


class WorkflowRunSummaryDto(BaseModel):
    id: str
    workflow_id: str
    workflow_version: int
    document_id: Optional[str] = None
    status: str
    error: Optional[dict[str, Any]] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


class WorkflowRunDto(WorkflowRunSummaryDto):
    # waiting（何を待っているか）だけを出す。trigger の graph_json スナップショットは
    # 大きいので API には出さない
    waiting: Optional[dict[str, Any]] = None
    node_runs: list[WorkflowNodeRunDto] = Field(default_factory=list)


class WorkflowRunList(BaseModel):
    items: list[WorkflowRunSummaryDto]


class ConnectionCreateRequest(BaseModel):
    """接続の登録（§16.5 / P6）。秘密は Secrets Manager に置き secret_ref（ARN）だけ渡す。"""

    type: str  # postgres / webhook / s3
    name: str
    config: dict[str, Any] = Field(default_factory=dict)
    secret_ref: Optional[str] = None
    allowed_tables: list[str] = Field(default_factory=list)


class ConnectionDto(BaseModel):
    id: str
    type: str
    name: str
    config: dict[str, Any] = Field(default_factory=dict)
    secret_ref: Optional[str] = None
    allowed_tables: list[str] = Field(default_factory=list)
    status: str
    created_at: Optional[str] = None


class ConnectionList(BaseModel):
    items: list[ConnectionDto]


class ConnectionTestResult(BaseModel):
    ok: bool
    status: str
    message: Optional[str] = None


class SinkPreviewDto(BaseModel):
    """dry-run の sink プレビュー（§9 / P6）。sql は実行側と同一実装で生成される。"""

    node_id: str
    node_type: str
    ok: bool
    connection_id: str
    sql: Optional[str] = None
    payload: Optional[dict[str, Any]] = None
    columns: list[str] = Field(default_factory=list)
    error: Optional[str] = None


class DryRunResult(BaseModel):
    ok: bool
    sinks: list[SinkPreviewDto]


class WebhookEndpointRequest(BaseModel):
    url: str
    # 署名鍵（§6.4 X-NF-Signature）。省略時はサーバが生成して 1 度だけ返す。
    secret: Optional[str] = None
    name: str = "webhook"


class WebhookEndpointDto(BaseModel):
    id: str
    name: str
    url: str
    status: str
    created_at: Optional[str] = None
    # 登録時のみ返す。以降は取得できない（保存しているのはサーバだけ）。
    secret: Optional[str] = None


class WebhookEndpointList(BaseModel):
    items: list[WebhookEndpointDto]


class PatchRuleRequest(BaseModel):
    status: str  # "active"（有効化）/ "retired"（退役）


class ChatRequest(BaseModel):
    message: str


class ChatConfirmRequest(BaseModel):
    action: str  # "update_schema" 等
    params: dict[str, Any] = Field(default_factory=dict)


class ChatConfirmResult(BaseModel):
    ok: bool
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)


class ClassifyCandidateDto(BaseModel):
    schema_id: str
    doc_type: str
    score: float


class ClassifyResponse(BaseModel):
    """帳票自動分類（⑦）。抽出前にファイル名等から最も近いスキーマを提案する。"""

    suggested_schema_id: Optional[str] = None
    doc_type: Optional[str] = None
    confidence: float = 0.0
    reason: str = ""
    # 判定の根拠種別: declared(指定済)/content(本文)/filename(ファイル名)/heuristic
    method: str = "heuristic"
    candidates: list[ClassifyCandidateDto] = Field(default_factory=list)


class MetricsResponse(BaseModel):
    total_documents: int
    status_counts: dict[str, int]
    stp_rate: float
    corrections_total: int
    active_rules: int
    pending_rules: int
    memories_total: int
    field_accuracy_sampled: Optional[float] = None
    llm_cost_jpy_total: Optional[float] = None
