"""graph_json のモデル（docs/design/16-agent-workflow.md §4.1〜4.2）。

未知のノード種別・未知の config キーは**保存時に弾く**（extra="forbid"）。
実行時に初めて落ちると、有効化済みのワークフローが本番で死ぬ。

`pos` は UI（SCR-07）専用で、実行エンジンは無視する。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from newfan_workflow.expr import parse_expr

# DD-12: テーブル名・列名は識別子であり SQL にバインドできない。モデルの段階で
# 形を縛る（psycopg.sql.Identifier での引用と allowed_tables の完全一致は実行側で重ねる）。
_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
_TABLE_PATTERN = rf"^{_IDENT}(?:\.{_IDENT})?$"
_COLUMN_PATTERN = rf"^{_IDENT}$"


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# ---------- トリガー（source.*） ----------


class S3EventConfig(_Cfg):
    """S3 イベント駆動トリガー（設計 v0.2 §7.2）。

    ポーリングはしない。S3 → EventBridge → SQS（保持 14 日）→ trigger consumer の
    経路で発火し、環境停止中のイベントは SQS に溜まって up 時に drain される。
    v0.1 の source.folder_watch（interval_sec ポーリング常駐）は「使う時だけ up」
    運用と両立しないため廃止した。
    """

    connection_id: str = Field(min_length=1)
    prefix: str = Field(min_length=1)
    extensions: list[str] = Field(
        default_factory=lambda: [".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"]
    )

    @field_validator("extensions")
    @classmethod
    def _dot_prefixed(cls, v: list[str]) -> list[str]:
        bad = [e for e in v if not e.startswith(".")]
        if bad:
            raise ValueError(f"拡張子は '.' 始まりで書いてください: {bad}")
        return [e.lower() for e in v]


class EmailAttachmentConfig(_Cfg):
    connection_id: str = Field(min_length=1)
    from_filter: Optional[str] = None
    subject_filter: Optional[str] = None


class _FolderEventConfig(_Cfg):
    """SaaS フォルダ監視トリガーの共通形（⑤⑥）。gdrive/m365/box が同じ設定を持つ。

    常駐は増やさない: orchestrator-worker 内のポーラーが up の間だけ interval おきに
    新着を検知する（source.schedule の分ティックと同型）。down 中の新着はファイルが
    SaaS 側に残っているため、次の up のポーリングで遡って取り込まれる
    （s3_event の SQS 滞留 drain と同じ「後追い」保証）。

    接続は対応する type（gdrive/m365/box）を指し、config.folder_id と認証の
    secret_ref は接続側が持つ（テナントはノード側で folder を意識しない）。
    """

    connection_id: str = Field(min_length=1)
    extensions: list[str] = Field(
        default_factory=lambda: [".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"]
    )

    @field_validator("extensions")
    @classmethod
    def _dot_prefixed(cls, v: list[str]) -> list[str]:
        bad = [e for e in v if not e.startswith(".")]
        if bad:
            raise ValueError(f"拡張子は '.' 始まりで書いてください: {bad}")
        return [e.lower() for e in v]


class GDriveEventConfig(_FolderEventConfig):
    """Google Drive フォルダ監視トリガー（接続 type=gdrive）。"""


class M365EventConfig(_FolderEventConfig):
    """Microsoft 365（OneDrive/SharePoint）フォルダ監視トリガー（接続 type=m365）。"""


class BoxEventConfig(_FolderEventConfig):
    """Box フォルダ監視トリガー（接続 type=box）。"""


class ManualConfig(_Cfg):
    """POST /documents / UI アップロード起点。設定は無い。"""


class ScheduleConfig(_Cfg):
    cron: str

    @field_validator("cron")
    @classmethod
    def _five_fields(cls, v: str) -> str:
        if len(v.split()) != 5:
            raise ValueError(f"cron は 5 フィールド（分 時 日 月 曜日）で書いてください: {v!r}")
        return v


# ---------- 処理（process.*） ----------


class ClassifyConfig(_Cfg):
    doc_types: list[str] = Field(min_length=1)
    on_unknown: Literal["default_route", "halt"] = "default_route"


class ExtractOptions(_Cfg):
    force_vl: bool = False


class ExtractConfig(_Cfg):
    # schema_id は必須。省略を許すと load_context が空スキーマを返し、KIE が
    # 1 項目も抽出しないまま「成功」する（実 AWS で踏んだ挙動）。
    schema_id: str = Field(min_length=1)
    options: ExtractOptions = Field(default_factory=ExtractOptions)


# ---------- 分岐（branch.*） ----------


class BranchArm(_Cfg):
    when: str
    to: str = Field(min_length=1)

    @field_validator("when")
    @classmethod
    def _valid_expr(cls, v: str) -> str:
        parse_expr(v)  # 文法・型違いは保存時に落とす（ExprError は ValueError）
        return v


class ConditionConfig(_Cfg):
    branches: list[BranchArm] = Field(min_length=1)
    # else 必須（lint L005 の前段としてモデルでも強制する）
    else_: str = Field(alias="else", min_length=1)


class HitlGateConfig(_Cfg):
    priority_boost: int = Field(default=0, ge=0, le=100)
    assignee_group: Optional[str] = None
    sla_hours: Optional[float] = Field(default=None, gt=0)


# ---------- 変換 / 出力 ----------


class FieldMapping(_Cfg):
    from_: Optional[str] = Field(default=None, alias="from")
    to: str = Field(min_length=1)
    format: Optional[str] = None
    const: Optional[str] = None
    mask: bool = False

    @model_validator(mode="after")
    def _source_exclusive(self) -> "FieldMapping":
        # from（抽出フィールド由来）と const（固定値）はどちらか一方。
        # 両方あると優先順位が暗黙になり、どちらも無いと値の出所が無い。
        if (self.from_ is None) == (self.const is None):
            raise ValueError("from と const はどちらか一方だけを指定してください")
        return self


class MapFieldsConfig(_Cfg):
    mappings: list[FieldMapping] = Field(min_length=1)


class DbWriteConfig(_Cfg):
    connection_id: str = Field(min_length=1)
    table: str = Field(pattern=_TABLE_PATTERN)
    mode: Literal["insert", "upsert"]
    keys: list[str] = Field(default_factory=list)
    on_failure: Literal["retry", "skip_and_notify", "halt_notify"] = "halt_notify"

    @field_validator("keys")
    @classmethod
    def _keys_are_identifiers(cls, v: list[str]) -> list[str]:
        import re

        bad = [k for k in v if not re.match(_COLUMN_PATTERN, k)]
        if bad:
            raise ValueError(f"キー列名が識別子ではありません: {bad}")
        return v

    @model_validator(mode="after")
    def _mode_keys(self) -> "DbWriteConfig":
        if self.mode == "upsert" and not self.keys:
            raise ValueError("upsert には keys（ON CONFLICT の対象列）が必要です")
        if self.mode == "insert" and self.keys:
            raise ValueError("insert で keys は使いません（upsert の指定漏れでは？）")
        return self


class WebhookSinkConfig(_Cfg):
    connection_id: str = Field(min_length=1)
    payload_template: Optional[str] = None


class FileSinkConfig(_Cfg):
    connection_id: str = Field(min_length=1)
    path: str = Field(min_length=1)
    format: Literal["json", "csv"] = "json"


class NotifyConfig(_Cfg):
    connection_id: str = Field(min_length=1)
    template: str = Field(min_length=1)
    when: Optional[str] = None

    @field_validator("when")
    @classmethod
    def _valid_expr(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            parse_expr(v)
        return v


# ---------- ノード ----------


class _NodeBase(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    id: str = Field(min_length=1)
    pos: Optional[tuple[float, float]] = None  # UI 専用。実行エンジンは無視する


class S3EventNode(_NodeBase):
    type: Literal["source.s3_event"]
    config: S3EventConfig


class GDriveEventNode(_NodeBase):
    type: Literal["source.gdrive_event"]
    config: GDriveEventConfig


class M365EventNode(_NodeBase):
    type: Literal["source.m365_event"]
    config: M365EventConfig


class BoxEventNode(_NodeBase):
    type: Literal["source.box_event"]
    config: BoxEventConfig


# フォルダ監視トリガー一式（lint / trigger 側の同型処理で使う）
FOLDER_EVENT_NODE_TYPES = (GDriveEventNode, M365EventNode, BoxEventNode)


class EmailAttachmentNode(_NodeBase):
    type: Literal["source.email_attachment"]
    config: EmailAttachmentConfig


class ManualNode(_NodeBase):
    type: Literal["source.manual"]
    config: ManualConfig = Field(default_factory=ManualConfig)


class ScheduleNode(_NodeBase):
    type: Literal["source.schedule"]
    config: ScheduleConfig


class ClassifyNode(_NodeBase):
    type: Literal["process.classify"]
    config: ClassifyConfig


class ExtractNode(_NodeBase):
    type: Literal["process.extract"]
    config: ExtractConfig


class ConditionNode(_NodeBase):
    type: Literal["branch.condition"]
    config: ConditionConfig


class HitlGateNode(_NodeBase):
    type: Literal["branch.hitl_gate"]
    config: HitlGateConfig = Field(default_factory=HitlGateConfig)


class MapFieldsNode(_NodeBase):
    type: Literal["transform.map_fields"]
    config: MapFieldsConfig


class DbWriteNode(_NodeBase):
    type: Literal["sink.db_write"]
    config: DbWriteConfig


class WebhookSinkNode(_NodeBase):
    type: Literal["sink.webhook"]
    config: WebhookSinkConfig


class FileSinkNode(_NodeBase):
    type: Literal["sink.file"]
    config: FileSinkConfig


class NotifyNode(_NodeBase):
    type: Literal["sink.notify"]
    config: NotifyConfig


WorkflowNode = Annotated[
    Union[
        S3EventNode,
        GDriveEventNode,
        M365EventNode,
        BoxEventNode,
        EmailAttachmentNode,
        ManualNode,
        ScheduleNode,
        ClassifyNode,
        ExtractNode,
        ConditionNode,
        HitlGateNode,
        MapFieldsNode,
        DbWriteNode,
        WebhookSinkNode,
        FileSinkNode,
        NotifyNode,
    ],
    Field(discriminator="type"),
]


class Edge(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    from_: str = Field(alias="from", min_length=1)
    to: str = Field(min_length=1)
    label: Optional[str] = None


class WorkflowGraph(BaseModel):
    """workflows.graph_json の全体。version は**この形式**の版で、

    ワークフロー自体の版（workflows.version、§11.1 の版固定）とは別物。
    未知の将来形式を黙って読み違えないよう Literal で固定する。
    """

    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    nodes: list[WorkflowNode] = Field(min_length=1)
    edges: list[Edge] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_ids(self) -> "WorkflowGraph":
        seen: set[str] = set()
        dupes: set[str] = set()
        for n in self.nodes:
            (dupes if n.id in seen else seen).add(n.id)
        if dupes:
            # 重複を許すと edges / branches の参照先が曖昧になり、実行が不定になる
            raise ValueError(f"node id が重複しています: {sorted(dupes)}")
        return self

    def get(self, node_id: str) -> Optional[WorkflowNode]:
        for n in self.nodes:
            if n.id == node_id:
                return n
        return None

    def node_ids(self) -> set[str]:
        return {n.id for n in self.nodes}


def is_trigger(node: WorkflowNode) -> bool:
    return node.type.startswith("source.")


def is_sink(node: WorkflowNode) -> bool:
    return node.type.startswith("sink.")


# ノード種別 → config モデル。SCR-07 の設定フォームはこの JSON Schema から
# 自動生成する（13 種を手書きすると UI の工数がエンジンを超える）。
NODE_CONFIG_MODELS: dict[str, type[BaseModel]] = {
    "source.s3_event": S3EventConfig,
    "source.gdrive_event": GDriveEventConfig,
    "source.m365_event": M365EventConfig,
    "source.box_event": BoxEventConfig,
    "source.email_attachment": EmailAttachmentConfig,
    "source.manual": ManualConfig,
    "source.schedule": ScheduleConfig,
    "process.classify": ClassifyConfig,
    "process.extract": ExtractConfig,
    "branch.condition": ConditionConfig,
    "branch.hitl_gate": HitlGateConfig,
    "transform.map_fields": MapFieldsConfig,
    "sink.db_write": DbWriteConfig,
    "sink.webhook": WebhookSinkConfig,
    "sink.file": FileSinkConfig,
    "sink.notify": NotifyConfig,
}


def catalog() -> dict[str, dict[str, Any]]:
    """ノード種別 → config の JSON Schema（ノードエディタのフォーム生成用）。"""
    return {t: m.model_json_schema() for t, m in NODE_CONFIG_MODELS.items()}
