"""API リクエスト/レスポンス DTO（§6.3）。"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from newfan_schemas import ExtractedField, RegionRect, TableResult


class DocumentCreated(BaseModel):
    document_id: str
    page_count: Optional[int]
    status: str


class DocumentDeleted(BaseModel):
    """DELETE /documents/{id} の受領書（§6.2）。

    件数を返すのは、学習例（correction_logs）まで消えることを UI が黙らずに
    伝えられるようにするため。204 にすると本文を返せず、web の request<T> も
    無条件に res.json() する。
    """

    document_id: str
    deleted: bool = True
    objects_deleted: int = 0
    corrections_deleted: int = 0
    runs_deleted: int = 0


class PageDim(BaseModel):
    """ページの正規寸法（前処理後 PNG の画素）。領域の正規化・逆正規化に使う。"""

    page_no: int
    width: Optional[int] = None
    height: Optional[int] = None


class DocumentMeta(BaseModel):
    document_id: str
    status: str
    doc_type: Optional[str] = None
    external_ref: Optional[str] = None
    page_count: Optional[int] = None
    # 未訪問ページの領域も正規化できるようにページ寸法を返す（設計 §6）。
    # **一覧 API と共用の型なので既定は空配列**であり、埋めるのは単体取得だけ。
    # 一覧で埋めると帳票 1 件ごとに pages を引く N+1 になる。
    pages: list[PageDim] = Field(default_factory=list)


class DocumentList(BaseModel):
    items: list[DocumentMeta]
    next_cursor: Optional[str] = None


class ExtractOptions(BaseModel):
    force_vl: bool = False
    two_model_check: bool = False
    language: str = "ja"


class ExtractRequest(BaseModel):
    schema_id: Optional[str] = None
    # レビュー待ちの帳票を取り直す（設計 §3.1）。既定 false のときの競合判定は
    # processing と needs_review の両方（外部連携の二重投入防止）だが、テンプレート化
    # 直後の再抽出は「自動発見 run が needs_review」が典型状態なので必ず 409 になる。
    # true のときだけ processing のみを競合とみなし、旧 needs_review run は
    # superseded へ落とす。confirmed / exported は true でも拒否する
    # （会計連携済みの確定値を無警告で置き換えないため）。
    supersede_review: bool = False
    options: ExtractOptions = Field(default_factory=ExtractOptions)


class ExtractAccepted(BaseModel):
    job_id: str
    run_id: str


class JobStatus(BaseModel):
    job_id: str
    kind: str
    status: str
    error_code: Optional[str] = None


class ResolvedRegion(BaseModel):
    """ページ番号まで解決済みの領域（検証画面のオーバーレイ用）。"""

    page_no: int
    rect: list[float]
    label: Optional[str] = None


class ResultResponse(BaseModel):
    document_id: str
    run_id: str
    status: str
    # スキーマレス抽出（自動発見, §5.5.1）なら None。UI はこれで「テンプレート化」
    # 導線を出し分ける（スキーマ指定済みの run に出しても意味がない）
    schema_id: Optional[str] = None
    result_version: int
    engine_versions: dict[str, Any]
    fields: list[ExtractedField]
    tables: list[TableResult]
    review_summary: dict[str, Any]
    fallback_pages: list[int] = Field(default_factory=list)  # VL フォールバックしたページ（§5.4）
    # 除外領域で消した件数（設計 §5.4）。検証画面のバッジの唯一の到達経路
    # （ReviewItem は永続化されないため）。0 件なら UI は出さない。
    region_stats: Optional[dict[str, Any]] = None
    # この run に適用された除外領域を**ページ解決済み**で返す（§6）。"last" / null の
    # 解決を web に再実装させると、最終ページ限定の承認印除外が全ページに描かれる
    # ／描かれない事故になる。
    applied_exclude_regions: list[ResolvedRegion] = Field(default_factory=list)
    # run.schema_id から解決した doc_type（編集モードのプリロード起点）。
    # list_schemas は doc_type ごと最新版しか返さず、run.schema_id は旧版であり得るので
    # id 突合ができない。
    schema_doc_type: Optional[str] = None


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
    # records.SchemaFieldDef と**同一コミットで**追加すること。pydantic v2 の既定は
    # extra="ignore" なので、DTO 側を忘れると GET /schemas の応答から region が
    # 静かに消え、旧編集画面の「新版として保存」往復で region が全滅する（§4.2）。
    region: Optional[RegionRect] = None


class SchemaDto(BaseModel):
    # id（field_schemas.id）は抽出やワークフローの schema_id に使う。UI がスキーマを
    # 選ばせるには id が要る（従来は doc_type しか返しておらず ID を手転記していた）。
    id: str
    doc_type: str
    version: int
    fields: list[SchemaFieldDto]
    exclude_regions: list[RegionRect] = Field(default_factory=list)
    source_page_count: Optional[int] = None


class SchemaList(BaseModel):
    items: list[SchemaDto]


class PutSchemaRequest(BaseModel):
    doc_type: str
    fields: list[SchemaFieldDto]
    # 新規作成モード: 既存 doc_type があれば E1005 で拒否する。クライアントの重複チェックは
    # 一覧が陳腐化していると素通りし、既存スキーマを黙って新版で置換してしまうため、
    # 「作成のつもり」はサーバ側で守る。省略時（false）は従来どおり常に新版作成（§7.2）。
    create: bool = False
    # **None = 直前版から引き継ぎ / 明示 [] = クリア**（設計 §4.4）。
    # 旧編集画面と chat 経路は body に exclude_regions を含めないため、「省略時 []」に
    # すると旧経路の保存 1 回で除外設定が全滅する。引き継ぎは put_schema の内部で
    # 行うので、旧経路はコード無変更のまま安全。
    exclude_regions: Optional[list[RegionRect]] = None
    source_page_count: Optional[int] = None


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
    # 最終同期の結果（⑤⑥。サイレント失敗の可視化）
    last_synced_at: Optional[str] = None
    last_sync_status: Optional[str] = None  # ok / error
    last_sync_error: Optional[str] = None


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


class CreateLlmHintRequest(BaseModel):
    """LLM最適化ヒント（llm_hint）を人が直接オーサリングする（③）。

    決定論変換（regex/vocab）と違い、抽出LLMへの自然言語指示。ゴールデン再現率の
    検証対象ではなく、admin が明示的に書いた指示として draft で作られ、承認で有効化する。
    """

    doc_type: str
    field_name: Optional[str] = None
    hint_text: str
    description: Optional[str] = None


class ChatRequest(BaseModel):
    message: str


class ChatConfirmRequest(BaseModel):
    action: str  # "update_schema" 等
    params: dict[str, Any] = Field(default_factory=dict)


class ChatConfirmResult(BaseModel):
    ok: bool
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)


class ConnectionSyncAccepted(BaseModel):
    """「今すぐ同期」（⑤⑥）。検知・取込は worker 側ポーラーが q.sync 経由で行う。"""

    queued: bool


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
