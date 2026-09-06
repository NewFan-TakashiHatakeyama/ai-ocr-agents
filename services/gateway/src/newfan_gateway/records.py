"""リポジトリのレコード型（DB 行のアプリ表現。§7 DDL に対応）。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field

from newfan_schemas import ExtractedField, RegionRect, TableResult


def _now() -> datetime:
    return datetime.now(timezone.utc)


class DocumentRecord(BaseModel):
    id: str
    tenant_id: str
    storage_uri: str
    original_name: Optional[str] = None
    mime_type: str
    page_count: Optional[int] = None
    doc_type: Optional[str] = None
    external_ref: Optional[str] = None
    status: str = "uploaded"
    created_at: datetime = Field(default_factory=_now)


class PageRecord(BaseModel):
    page_no: int
    width: Optional[int] = None
    height: Optional[int] = None
    image_uri: str
    preproc: dict[str, Any] = Field(default_factory=dict)


class RunRecord(BaseModel):
    id: str
    tenant_id: str
    document_id: str
    schema_id: Optional[str] = None
    status: str = "processing"
    engine_versions: dict[str, Any] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)
    result_version: int = 1
    fields: list[ExtractedField] = Field(default_factory=list)
    tables: list[TableResult] = Field(default_factory=list)
    review_summary: dict[str, Any] = Field(default_factory=dict)
    # VL フォールバックしたページ番号（品質ゲート NG, §5.4/DD-09）。UI のバッジ/バナー用。
    fallback_pages: list[int] = Field(default_factory=list)
    # 除外領域で消した件数（metrics.region）。UI のバッジ用。
    region_stats: Optional[dict[str, Any]] = None
    started_at: datetime = Field(default_factory=_now)


class JobRecord(BaseModel):
    id: str
    tenant_id: str
    kind: str
    ref_id: str
    status: str = "queued"
    error_code: Optional[str] = None


class CorrectionRecord(BaseModel):
    """§7 correction_logs の 1 行。列は DDL と一致させる。

    doc_type/supplier_key/context は learn ノードが memory へ渡す検索キーと
    embedding 入力（DD-06/DD-07）。ここで埋めないと学習ループに何も伝わらない。
    """

    id: str
    tenant_id: str
    document_id: str
    run_id: str
    field_name: str
    original_value: Optional[str] = None
    corrected_value: str
    doc_type: Optional[str] = None
    supplier_key: Optional[str] = None
    context: Optional[str] = None
    reviewer_id: Optional[str] = None
    created_at: datetime = Field(default_factory=_now)


# ---- 管理画面（SCR-04/05/06） ----


class SchemaFieldDef(BaseModel):
    name: str
    label: Optional[str] = None
    type: str = "string"
    required: bool = False
    critical: bool = False
    columns: Optional[list[dict[str, Any]]] = None  # table 型の列定義（§5.5）
    # 読取領域（設計 §4.2）。newfan_schemas に一元定義したものを import する
    # （gateway で再定義すると検証規則が二重管理になり、片方だけ緩む）。
    region: Optional[RegionRect] = None


class SchemaRecord(BaseModel):
    """field_schemas 行（§7.2）。有効版＝doc_type ごとの最新 version。"""

    id: str
    tenant_id: str
    doc_type: str
    version: int
    fields: list[SchemaFieldDef] = Field(default_factory=list)
    # 除外領域（設計 §4.3 migration 0007）。fields JSONB 内に擬似フィールドとして
    # 置かないのは、classify の分類語彙を汚染しスキーマ編集画面にも並ぶため。
    exclude_regions: list[RegionRect] = Field(default_factory=list)
    # テンプレート化時点の帳票ページ数。位置ガードの page 判定に使う（§5.5）。
    source_page_count: Optional[int] = None
    updated_at: datetime = Field(default_factory=_now)


class WorkflowRecord(BaseModel):
    """workflows 行（§16 設計 v0.2）。

    graph_json は newfan_workflow.WorkflowGraph で検証済みの dict を保持する。
    version はワークフロー定義の版（graph_json の形式版とは別物）。
    実行中の workflow_run は開始時点の版を参照し続ける（§11.1 版の固定）。
    """

    id: str
    tenant_id: str
    name: str
    status: str = "draft"  # draft / active / paused / retired
    version: int = 1
    graph_json: dict[str, Any] = Field(default_factory=dict)
    auto_confirm: bool = False
    created_by: Optional[str] = None
    updated_at: Optional[str] = None


class WorkflowRunRecord(BaseModel):
    """workflow_runs 行（§16 設計 v0.2 §3）。trigger.graph_json が実行時の版スナップショット。"""

    id: str
    tenant_id: str
    workflow_id: str
    workflow_version: int = 1
    document_id: Optional[str] = None
    trigger: dict[str, Any] = Field(default_factory=dict)
    state: dict[str, Any] = Field(default_factory=dict)
    status: str = "running"
    error: Optional[dict[str, Any]] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


class WorkflowNodeRunRecord(BaseModel):
    node_id: str
    node_type: str
    status: str = "pending"
    attempt: int = 0
    output: Optional[dict[str, Any]] = None
    error: Optional[dict[str, Any]] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


class ConnectionRecord(BaseModel):
    """connections 行（§6.4 Webhook / §16.5 接続管理）。

    secret は config に入るが API では返さない（登録時に一度だけ利用者が持つ）。
    """

    id: str
    tenant_id: str
    type: str
    name: str
    config: dict[str, Any] = Field(default_factory=dict)
    # 秘密は Secrets Manager 参照のみ（§16.5 / P6）。値そのものは持たない
    secret_ref: Optional[str] = None
    allowed_tables: list[str] = Field(default_factory=list)
    status: str = "untested"
    created_at: Optional[str] = None
    # 最終同期の結果（⑤⑥ フォルダ監視系。worker のポーラーが書く）。
    # UI で「取り込まれないのに気付けない」サイレント故障を可視化する
    last_synced_at: Optional[str] = None
    last_sync_status: Optional[str] = None  # ok / error
    last_sync_error: Optional[str] = None


class MemoryRecord(BaseModel):
    """tenant_memories × correction_logs（§5.8）。

    tenant_memories 単体は faiss_vector_id しか持たず人が読んでも何も分からない。
    「何をどう直したから学習されたのか」を見せるため correction_logs と結合した形で扱う。
    """

    id: str
    tenant_id: str
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


class RuleRecord(BaseModel):
    """tenant_rules 行（§5.8.4）。"""

    id: str
    tenant_id: str
    doc_type: Optional[str] = None
    supplier_key: Optional[str] = None
    field_name: Optional[str] = None
    rule_type: str
    rule_json: dict[str, Any] = Field(default_factory=dict)
    status: str = "draft"
    validation_report: Optional[dict[str, Any]] = None
    source_correction_ids: list[str] = Field(default_factory=list)
    created_by: str = "agent"
    updated_at: datetime = Field(default_factory=_now)


class MetricsSummary(BaseModel):
    """ダッシュボード KPI（§12.1 と対応。未計測項目は None）。"""

    total_documents: int = 0
    status_counts: dict[str, int] = Field(default_factory=dict)
    stp_rate: float = 0.0
    corrections_total: int = 0
    active_rules: int = 0
    pending_rules: int = 0
    memories_total: int = 0
    field_accuracy_sampled: Optional[float] = None  # 確定 Run の無修正フィールド率（直近30日）
    llm_cost_jpy_total: Optional[float] = None  # 実測トークン×単価の合計（直近30日）
