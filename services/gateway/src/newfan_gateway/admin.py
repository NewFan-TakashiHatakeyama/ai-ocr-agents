"""管理画面（SCR-04/05/06）の Repository 抽象と In-Memory 実装。

スキーマ版管理（§5.5）、ルールライフサイクル（§5.8.4）、KPI 集計（§12.1）。
本番は db.PgAdminRepository を注入。エンドポイントは本 Protocol のみに依存する。
"""

from __future__ import annotations

from typing import Any, Iterable, Optional, Protocol

from newfan_schemas import RegionRect, check_field_name, check_field_type

from newfan_gateway.ids import new_id
from newfan_gateway.sentinels import UNSET
from newfan_gateway.records import (
    ConnectionRecord,
    MemoryRecord,
    MetricsSummary,
    RuleRecord,
    SchemaFieldDef,
    SchemaRecord,
    SchemaVersionRef,
)

def check_field_defs(fields: list[SchemaFieldDef]) -> None:
    """予約名（__pages__ / __region__ / 先頭 __。設計 D9）と、``FieldType`` に無い型を拒む。

    **書き込み側の共通入口。** InMemory / Pg 両方の put_schema がここを通るので、
    ルータ経由でもチャット経由（admin.put_schema 直呼び）でも予約名・未知の型は保存
    されない。型を見るのは、orchestrator が実行時に ``FieldSchema``（``type: FieldType``）
    で落とすのを保存時に前倒しするため ── 知らない型が保存されると、その doc_type の
    抽出は全部 failed（E9001）になる（ADR-0007 の ``address_jp`` の展開順の事故）。
    読み出しモデル（records.SchemaFieldDef / newfan_schemas.FieldDef）には置かない ──
    検査導入前に保存された旧データを読めなくすると、同テナントの健全なスキーマまで
    巻き込んで一覧が落ちる。
    """
    for f in fields:
        check_field_name(f.name)
        check_field_type(f.type)


def archived_schema_message(doc_type: str, *, action: str = "使う") -> str:
    """アーカイブ済みスキーマを断るときの文言（C9-D）。REST（E1005）と chat（ok=False）で
    同じ言葉にする——「先に復元してください」が次の一手で、画面のバッジと対応する。"""
    return f"スキーマ「{doc_type}」はアーカイブ済みです。{action}には先に復元してください"


class SchemaVersionConflictError(ValueError):
    """同じ doc_type への同時保存で版番号が衝突した（第 3 回敵対的レビュー 1）。

    Pg 実装は advisory lock で同時保存を直列化するので通常は起きないが、ロックを
    経由しない書き込み（seed スクリプト等）と重なった場合の最後の防波堤。ルータは
    E1005（再読み込みしてやり直し）に翻訳する。ValueError 派生の理由は
    SchemaArchivedError と同じ（chat 経路が ok=False で会話を続けられるように）。
    """

    def __init__(self, doc_type: str) -> None:
        super().__init__(
            f"スキーマ「{doc_type}」が同時に保存されました。再読み込みしてからやり直してください"
        )
        self.doc_type = doc_type


class SchemaArchivedError(ValueError):
    """アーカイブ済み doc_type への書き込み（新版作成）を put_schema が拒む（C9-D）。

    ValueError の派生にするのは reject_reserved_field_names と同じ理由: chat 経路
    （chat_tools.update_schema）は ValueError を ok=False の応答に変えて会話を
    続けるので、ここで別系統の例外にするとグラフごと落ちる。ルータは E1005 に翻訳する。
    アーカイブ済みに新版を足すと、その版だけ is_active=true で「一覧に戻る」ため、
    アーカイブが黙って解除される。復元（unarchive）を明示的に踏ませる。
    """

    def __init__(self, doc_type: str) -> None:
        super().__init__(archived_schema_message(doc_type, action="編集する"))
        self.doc_type = doc_type


# ルール有効化の閾値（§2.5 rules.validation_pass: 再現率≥90% かつ 回帰0件）
MIN_REPRODUCTION = 0.9


def is_activatable(report: Optional[dict[str, Any]]) -> bool:
    if not report:
        return False
    repro = float(report.get("reproduction_rate", 0.0) or 0.0)
    regressions = int(report.get("regressions", 1) or 0)
    return repro >= MIN_REPRODUCTION and regressions == 0


ACTIVATION_BLOCKED_MESSAGE = "検証未達のため有効化できません（再現率≥90%・回帰0件が必要）"


def can_activate(rec: RuleRecord) -> bool:
    """ルールを active にしてよいか（§5.8.4）。

    決定論変換（regex/vocab 等）は検証合格（再現率≥90%・回帰0件）が条件。ただし
    「人が明示的に書いた」llm_hint は検証対象ではないため条件を課さない。学習
    エージェント生成の llm_hint（created_by="agent"）は従来どおりゲート対象
    （無検証ルールの1クリック注入を防ぐ）。

    PATCH /rules/{id} とチャット承認（manage_rules）の**共通判定**。片方だけ緩むと
    チャットが検証ゲートの抜け道になる。
    """
    human_hint = rec.rule_type == "llm_hint" and rec.created_by != "agent"
    return human_hint or is_activatable(rec.validation_report)


class AdminRepository(Protocol):
    # スキーマ（§5.5）
    def list_schemas(
        self, tenant_id: str, *, include_archived: bool = False
    ) -> list[SchemaRecord]:
        """doc_type ごとの最新版。既定ではアーカイブ済みを含めない（C9-D）。

        呼び出し側（抽出のスキーマ選択・分類候補・doc-types・ワークフローの extract
        ノード）はすべて「今使えるもの」を求めているので、既定で隠す。管理画面の
        「アーカイブ済みを表示」だけが include_archived=True を渡す。
        """
        ...

    def get_schema(self, tenant_id: str, doc_type: str) -> Optional[SchemaRecord]:
        """doc_type の最新版。アーカイブ済みでも返す（archived=True）。

        隠すかどうかは呼び出し側が決める（PUT は E1005、GET は E1001、chat は
        ok=False）。ここで None にすると「無い」と「アーカイブ済み」が区別できず、
        create=True の PUT が同名の新版を作ってアーカイブを黙って解除する。
        """
        ...

    def get_schema_by_id(self, tenant_id: str, schema_id: str) -> Optional[SchemaRecord]: ...
    def schema_ids_for_doc_type(self, tenant_id: str, doc_type: str) -> list[str]:
        """doc_type の全版の id（版の昇順）。ワークフローの extract.schema_id は旧版を
        固定保持し得るので、アーカイブのガードは全版で参照を探す。"""
        ...

    def schema_versions(
        self, tenant_id: str, schema_ids: Iterable[str]
    ) -> dict[str, SchemaVersionRef]:
        """schema_id → その版番号と当該 doc_type の最新版。**何件渡しても 1 回で引く。**

        旧版参照（ワークフローの extract.schema_id が最新版でない）の判定材料。
        ワークフロー一覧の旧版バッジは全ワークフローの schema_id をまとめて渡す
        （ワークフロー数に比例したクエリにしない）。stale-workflows も同じものを使う。
        「最新版」の定義は lint L012 の ``schema_is_latest`` と同じ（version 最大、
        is_active は見ない）。存在しない id は結果に載せない（「最新でない」ではなく
        「存在しない」であり、それは L009 が出す）。
        """
        ...

    def set_schema_archived(
        self, tenant_id: str, doc_type: str, archived: bool
    ) -> Optional[SchemaRecord]:
        """全版の is_active を一括で更新し、更新後の最新版を返す。doc_type が無ければ None。

        行は消さない（extraction_runs.schema_id の FK と過去の抽出結果の定義を保つ）。
        参照ガード（active なワークフローの extract.schema_id）はルータが行う。
        """
        ...
    def put_schema(
        self,
        tenant_id: str,
        doc_type: str,
        fields: list[SchemaFieldDef],
        *,
        exclude_regions: Optional[list[RegionRect]] = None,
        source_page_count: Optional[int] | Any = UNSET,
    ) -> SchemaRecord:
        """新版を INSERT する（常に新 version）。

        exclude_regions は **None = 直前版から引き継ぎ / 明示 [] = クリア**、
        source_page_count は **UNSET（省略）= 引き継ぎ / 明示 None = クリア**
        （設計 §4.4）。旧編集画面と chat 経路はこれらを送らないため、「省略時 []」に
        すると旧経路の保存 1 回で除外設定が全滅する。引き継ぎを呼び出し側でなく
        実装側に置くことで、旧経路はコード無変更のまま安全になる。戻り値には
        **引き継ぎ後の実値**を載せる（PUT 応答が直後の GET と一致しないと、旧画面が
        空配列で state を上書きして次の保存で本物のクリアを送ってしまう）。

        同じ doc_type への同時保存は実装側で直列化する（Pg は advisory lock）。
        衝突を検出したら SchemaVersionConflictError。
        """
        ...

    # ルール（§5.8.4）
    def create_rule(self, rec: RuleRecord) -> RuleRecord: ...
    def list_rules(
        self, tenant_id: str, *, status: Optional[str] = None, doc_type: Optional[str] = None
    ) -> list[RuleRecord]: ...
    def get_rule(self, tenant_id: str, rule_id: str) -> Optional[RuleRecord]: ...
    def set_rule_status(
        self, tenant_id: str, rule_id: str, status: str
    ) -> Optional[RuleRecord]: ...

    # 修正メモリ（§5.8 / DD-06）
    def list_memories(
        self,
        tenant_id: str,
        *,
        doc_type: Optional[str] = None,
        field_name: Optional[str] = None,
        limit: int = 50,
    ) -> list[MemoryRecord]: ...

    # Webhook 配信先（§6.4）。connections(type='webhook') 行として持つ。
    # secret_ref があれば署名鍵は Secrets Manager 参照（P6。secret は保存しない）
    def add_webhook_endpoint(
        self,
        tenant_id: str,
        *,
        url: str,
        secret: Optional[str],
        name: str,
        secret_ref: Optional[str] = None,
    ) -> ConnectionRecord: ...
    def list_webhook_endpoints(self, tenant_id: str) -> list[ConnectionRecord]: ...

    # 接続管理（§16.5 / P6）
    def create_connection(
        self,
        tenant_id: str,
        *,
        type: str,
        name: str,
        config: dict[str, Any],
        secret_ref: Optional[str],
        allowed_tables: list[str],
    ) -> ConnectionRecord: ...
    def list_connections(self, tenant_id: str) -> list[ConnectionRecord]: ...
    def get_connection(self, tenant_id: str, connection_id: str) -> Optional[ConnectionRecord]: ...
    def set_connection_status(
        self, tenant_id: str, connection_id: str, status: str
    ) -> Optional[ConnectionRecord]: ...
    def delete_connection(
        self, tenant_id: str, connection_id: str
    ) -> Optional[dict[str, int]]:
        """接続を消す（C9-D）。消したら件数の dict、無い/参照されていて消せなければ None。

        ワークフロー（現在の定義と run のスナップショット）から参照されている接続は
        消さない。ルータが先にガードして「誰が使っているか」を返すが、Pg 実装は
        DELETE 文自体にも同じ条件を含める（ガードと削除が別トランザクションのため、
        その間に有効化された参照を黙って壊さない）。
        source_cursors（フォルダ監視の重複排除台帳）は接続と一緒に消える。接続が
        無くなれば台帳は引かれず、残すと孤児になるだけ。件数は監査へ載せる。
        """
        ...

    # KPI（§12.1）
    def metrics_summary(self, tenant_id: str) -> MetricsSummary: ...


class InMemoryAdminRepository:
    def __init__(self) -> None:
        self._schemas: dict[str, SchemaRecord] = {}
        self._rules: dict[str, RuleRecord] = {}
        self._memories: list[MemoryRecord] = []
        self._connections: list[ConnectionRecord] = []
        self._metrics: dict[str, MetricsSummary] = {}

    # --- schemas ---
    def seed_schema(self, rec: SchemaRecord) -> None:
        self._schemas[rec.id] = rec

    def list_schemas(
        self, tenant_id: str, *, include_archived: bool = False
    ) -> list[SchemaRecord]:
        latest: dict[str, SchemaRecord] = {}
        for s in self._schemas.values():
            if s.tenant_id != tenant_id:
                continue
            cur = latest.get(s.doc_type)
            if cur is None or s.version > cur.version:
                latest[s.doc_type] = s
        return sorted(
            (s for s in latest.values() if include_archived or not s.archived),
            key=lambda s: s.doc_type,
        )

    def get_schema_by_id(self, tenant_id: str, schema_id: str) -> Optional[SchemaRecord]:
        rec = self._schemas.get(schema_id)
        return rec if rec and rec.tenant_id == tenant_id else None

    def get_schema(self, tenant_id: str, doc_type: str) -> Optional[SchemaRecord]:
        rows = self._versions(tenant_id, doc_type)
        return max(rows, key=lambda s: s.version) if rows else None

    def _versions(self, tenant_id: str, doc_type: str) -> list[SchemaRecord]:
        return [
            s for s in self._schemas.values() if s.tenant_id == tenant_id and s.doc_type == doc_type
        ]

    def schema_ids_for_doc_type(self, tenant_id: str, doc_type: str) -> list[str]:
        return [s.id for s in sorted(self._versions(tenant_id, doc_type), key=lambda s: s.version)]

    def schema_versions(
        self, tenant_id: str, schema_ids: Iterable[str]
    ) -> dict[str, SchemaVersionRef]:
        out: dict[str, SchemaVersionRef] = {}
        for sid in dict.fromkeys(schema_ids):
            rec = self.get_schema_by_id(tenant_id, sid)
            latest = self.get_schema(tenant_id, rec.doc_type) if rec is not None else None
            if rec is None or latest is None:
                continue
            out[sid] = SchemaVersionRef(
                id=rec.id,
                doc_type=rec.doc_type,
                version=rec.version,
                latest_schema_id=latest.id,
                latest_version=latest.version,
            )
        return out

    def set_schema_archived(
        self, tenant_id: str, doc_type: str, archived: bool
    ) -> Optional[SchemaRecord]:
        rows = self._versions(tenant_id, doc_type)
        if not rows:
            return None
        for s in rows:
            # 全版を同じ状態にする（Pg の UPDATE ... WHERE doc_type=:d と同じ意味論）
            s.archived = archived
        return self.get_schema(tenant_id, doc_type)

    def put_schema(
        self,
        tenant_id: str,
        doc_type: str,
        fields: list[SchemaFieldDef],
        *,
        exclude_regions: Optional[list[RegionRect]] = None,
        source_page_count: Optional[int] | Any = UNSET,
    ) -> SchemaRecord:
        check_field_defs(fields)  # 予約名（D9）と未知の型は書き込み側で拒む（読み出しでは拒まない）
        prev = self.get_schema(tenant_id, doc_type)
        if prev is not None and prev.archived:
            # 書き込み側の共通入口で拒む（D9 と同じ置き場所）。新版を足すとアーカイブが
            # 黙って解除される。
            raise SchemaArchivedError(doc_type)
        # None = 引き継ぎ（§4.4）。Pg 実装と意味論を揃える。
        regions = (
            list(exclude_regions)
            if exclude_regions is not None
            else (list(prev.exclude_regions) if prev else [])
        )
        # UNSET = 引き継ぎ / None = クリア（Pg 実装と同じ意味論）
        pages = (
            (prev.source_page_count if prev else None)
            if source_page_count is UNSET
            else source_page_count
        )
        rec = SchemaRecord(
            id=new_id("schema"),
            tenant_id=tenant_id,
            doc_type=doc_type,
            version=(prev.version + 1) if prev else 1,
            fields=fields,
            exclude_regions=regions,
            source_page_count=pages,
        )
        self._schemas[rec.id] = rec
        return rec

    # --- rules ---
    def seed_rule(self, rec: RuleRecord) -> None:
        self._rules[rec.id] = rec

    def create_rule(self, rec: RuleRecord) -> RuleRecord:
        self._rules[rec.id] = rec
        return rec

    def list_rules(
        self, tenant_id: str, *, status: Optional[str] = None, doc_type: Optional[str] = None
    ) -> list[RuleRecord]:
        return [
            r
            for r in self._rules.values()
            if r.tenant_id == tenant_id
            and (status is None or r.status == status)
            and (doc_type is None or r.doc_type in (None, doc_type))
        ]

    def get_rule(self, tenant_id: str, rule_id: str) -> Optional[RuleRecord]:
        r = self._rules.get(rule_id)
        return r if r and r.tenant_id == tenant_id else None

    def set_rule_status(self, tenant_id: str, rule_id: str, status: str) -> Optional[RuleRecord]:
        r = self.get_rule(tenant_id, rule_id)
        if r is None:
            return None
        r.status = status
        return r

    # --- 修正メモリ（§5.8 / DD-06） ---
    def seed_memory(self, rec: MemoryRecord) -> None:
        self._memories.append(rec)

    def list_memories(
        self,
        tenant_id: str,
        *,
        doc_type: Optional[str] = None,
        field_name: Optional[str] = None,
        limit: int = 50,
    ) -> list[MemoryRecord]:
        rows = [
            m
            for m in self._memories
            if m.tenant_id == tenant_id
            and (doc_type is None or m.doc_type == doc_type)
            and (field_name is None or m.field_name == field_name)
        ]
        # 新しい順。学習は追記なので、直近何を覚えたかが最も知りたい情報。
        rows.sort(key=lambda m: m.created_at or "", reverse=True)
        return rows[:limit]

    # --- webhook 配信先（§6.4） ---
    def add_webhook_endpoint(
        self,
        tenant_id: str,
        *,
        url: str,
        secret: Optional[str],
        name: str,
        secret_ref: Optional[str] = None,
    ) -> ConnectionRecord:
        # secret_ref があれば config に秘密を入れない（§16.5）。無ければ旧方式（平文）
        config = {"url": url} if secret_ref else {"url": url, "secret": secret}
        rec = ConnectionRecord(
            id=new_id("connection"),
            tenant_id=tenant_id,
            type="webhook",
            name=name,
            config=config,
            secret_ref=secret_ref,
            # 疎通確認前は untested。export の list_webhook_endpoints は
            # active/tested しか拾わないため、登録しただけでは配信されない（§16.5 の安全策）。
            status="untested",
        )
        self._connections.append(rec)
        return rec

    # --- 接続管理（§16.5 / P6） ---
    def create_connection(
        self,
        tenant_id: str,
        *,
        type: str,
        name: str,
        config: dict[str, Any],
        secret_ref: Optional[str],
        allowed_tables: list[str],
    ) -> ConnectionRecord:
        rec = ConnectionRecord(
            id=new_id("connection"),
            tenant_id=tenant_id,
            type=type,
            name=name,
            config=dict(config),
            secret_ref=secret_ref,
            allowed_tables=list(allowed_tables),
            status="untested",
        )
        self._connections.append(rec)
        return rec

    def list_connections(self, tenant_id: str) -> list[ConnectionRecord]:
        return [c for c in self._connections if c.tenant_id == tenant_id]

    def get_connection(self, tenant_id: str, connection_id: str) -> Optional[ConnectionRecord]:
        for c in self._connections:
            if c.tenant_id == tenant_id and c.id == connection_id:
                return c
        return None

    def set_connection_status(
        self, tenant_id: str, connection_id: str, status: str
    ) -> Optional[ConnectionRecord]:
        rec = self.get_connection(tenant_id, connection_id)
        if rec is None:
            return None
        updated = rec.model_copy(update={"status": status})
        self._connections[self._connections.index(rec)] = updated
        return updated

    def delete_connection(
        self, tenant_id: str, connection_id: str
    ) -> Optional[dict[str, int]]:
        # InMemory はワークフロー表を持たないので参照ガードはルータ側に任せる
        # （Pg 実装は DELETE 文に NOT EXISTS で同条件を含める）。
        rec = self.get_connection(tenant_id, connection_id)
        if rec is None:
            return None
        self._connections.remove(rec)
        return {"cursors_deleted": 0}

    def list_webhook_endpoints(self, tenant_id: str) -> list[ConnectionRecord]:
        return [c for c in self._connections if c.tenant_id == tenant_id and c.type == "webhook"]

    # --- metrics ---
    def set_metrics(self, tenant_id: str, m: MetricsSummary) -> None:
        self._metrics[tenant_id] = m

    def metrics_summary(self, tenant_id: str) -> MetricsSummary:
        if tenant_id in self._metrics:
            return self._metrics[tenant_id]
        active = sum(1 for r in self._rules.values() if r.tenant_id == tenant_id and r.status == "active")
        pending = sum(
            1
            for r in self._rules.values()
            if r.tenant_id == tenant_id and r.status in ("draft", "validating")
        )
        return MetricsSummary(active_rules=active, pending_rules=pending)
