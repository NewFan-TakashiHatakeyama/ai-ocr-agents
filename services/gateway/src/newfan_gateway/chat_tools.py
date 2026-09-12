"""チャットグラフのツール実体（§3.3 / §4.5）。

設計書のツール: get_result / explain_field / rerun_extract / update_schema /
search_documents / manage_rules。読み取り系は即実行、書込み系（rerun_extract /
update_schema / manage_rules）は confirm_action を経てから実行する。

グラフから DB/キューへ触るための薄い層。Repository/AdminRepository/Queue は
呼び出し側が注入する（テストは InMemory 実装）。
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import ValidationError

from newfan_gateway.admin import (
    ACTIVATION_BLOCKED_MESSAGE,
    AdminRepository,
    archived_schema_message,
    can_activate,
)
from newfan_gateway.ids import new_id
from newfan_gateway.queue import Queue
from newfan_gateway.records import JobRecord, RunRecord, SchemaFieldDef
from newfan_gateway.repository import Repository

# 書込み系。実行前に必ずユーザー確認を挟む（§3.3「書込み系ツールは実行前にユーザー確認ステップを必須」）
WRITE_TOOLS = frozenset({"rerun_extract", "update_schema", "manage_rules"})

# 承認実行（POST /chat/confirm）に要る最低ロール。同じ操作の非チャット API と揃える:
#   rerun_extract → POST /documents/{id}/extract（uploader）
#   update_schema → PUT /admin/schemas（admin）
#   manage_rules  → PATCH /rules/{id}（admin）
# チャット経路だけ緩いと、承認カードが権限昇格の抜け道になる。
WRITE_TOOL_MIN_ROLE: dict[str, str] = {
    "rerun_extract": "uploader",
    "update_schema": "admin",
    "manage_rules": "admin",
}


def usable_schema_error(admin: AdminRepository, tenant_id: str, schema_id: str) -> Optional[str]:
    """新しい抽出 run に schema_id を使えないときの文言。使えれば None。

    存在しない id と、アーカイブ済み（C9-D）の両方を断る。get_schema_by_id は過去の run
    から定義を辿るためにアーカイブ済みの行も返すので、ここで archived を見ないと
    「一覧から消えたスキーマで取り直す」がチャット経由で成立する。REST の /extract
    （routers._require_usable_schema）と同じ判定・同じ文言。
    """
    rec = admin.get_schema_by_id(tenant_id, schema_id)
    if rec is None:
        return f"スキーマが見つかりません: {schema_id}"
    if rec.archived:
        return archived_schema_message(rec.doc_type)
    return None


def field_validation_message(exc: ValidationError) -> str:
    """LLM 由来の項目定義が SchemaFieldDef の検証に落ちたときの利用者向け文言。

    pydantic の message は ``Value error, 項目名「__x」は予約されています…`` のように
    英語の種別が頭に付く。チャットにそのまま流すと読みづらいので、validator が
    投げた本文だけを取り出し、複数あれば「；」で繋ぐ。
    """
    parts: list[str] = []
    for e in exc.errors(include_url=False):
        msg = str(e.get("msg", ""))
        if msg.startswith("Value error, "):
            msg = msg[len("Value error, ") :]
        loc = ".".join(str(p) for p in e.get("loc", ()))
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "項目の定義が不正です: " + ("；".join(parts) if parts else "形式を確認してください")


class ChatTools:
    def __init__(self, *, repo: Repository, admin: AdminRepository, queue: Queue) -> None:
        self._repo = repo
        self._admin = admin
        self._queue = queue

    # ---------- 読み取り ----------

    def get_result(self, tenant_id: str, document_id: str) -> dict[str, Any]:
        """確定/抽出結果の要約を返す。"""
        run = self._repo.get_latest_run(tenant_id, document_id)
        if run is None:
            return {"error": "この文書の抽出結果がありません"}
        return {
            "document_id": document_id,
            "run_id": run.id,
            "status": run.status,
            "fields": [
                {
                    "name": f.name,
                    "value": f.value_normalized or f.value_raw,
                    "confidence": f.confidence,
                    "review_status": getattr(f.review_status, "value", f.review_status),
                }
                for f in run.fields
            ],
            "table_count": len(run.tables),
        }

    def explain_field(
        self, tenant_id: str, document_id: str, field_name: str
    ) -> dict[str, Any]:
        """項目の grounding（どこから読んだか）を返す（§3.3）。"""
        run = self._repo.get_latest_run(tenant_id, document_id)
        if run is None:
            return {"error": "この文書の抽出結果がありません"}
        for f in run.fields:
            if f.name == field_name:
                return {
                    "name": f.name,
                    "value": f.value_normalized or f.value_raw,
                    "confidence": f.confidence,
                    "grounding_score": f.grounding_score,
                    "page": f.page,
                    "bbox": f.bbox,
                    "source_quote": f.source_quote,
                }
        known = [f.name for f in run.fields]
        return {"error": f"項目 {field_name} は見つかりません", "available": known}

    def search_documents(
        self, tenant_id: str, *, status: Optional[str] = None, limit: int = 20
    ) -> dict[str, Any]:
        rows, _ = self._repo.list_documents(
            tenant_id, status=status, cursor=None, limit=min(limit, 50)
        )
        return {
            "count": len(rows),
            "items": [
                {
                    "document_id": d.id,
                    # 原本ファイル名。無いと LLM は ID しか言えず、利用者は
                    # 「どの帳票の話か」を突き合わせられない
                    "original_name": d.original_name,
                    "status": d.status,
                    "doc_type": d.doc_type,
                    "external_ref": d.external_ref,
                }
                for d in rows
            ],
        }

    def list_rules(self, tenant_id: str, *, status: Optional[str] = None) -> dict[str, Any]:
        rules = self._admin.list_rules(tenant_id, status=status)
        return {
            "count": len(rules),
            "items": [
                {
                    "rule_id": r.id,
                    "status": r.status,
                    "doc_type": r.doc_type,
                    "field_name": r.field_name,
                    "rule_type": r.rule_type,
                }
                for r in rules
            ],
        }

    # ---------- 書込み（confirm_action の後で呼ばれる） ----------

    def rerun_extract(
        self,
        tenant_id: str,
        document_id: str,
        *,
        schema_id: Optional[str] = None,
        options: Optional[dict[str, Any]] = None,
        supersede_review: bool = True,
    ) -> dict[str, Any]:
        """新しい Run を発行して job_id を返す（§4.5）。

        supersede_review=True（チャットの既定）は REST ``POST /documents/{id}/extract`` の
        ``supersede_review: true`` と同じ意味論:
          - 競合は「処理中（processing）」のみ。needs_review は拒否せず **superseded に
            終端させて**取り直す（残すと get_latest_run・削除ブロッカー・ワークフローの
            hitl_gate が旧 run を見続ける）。
          - 確定済み（confirmed / exported）は拒否する（確定値の無警告置換防止）。
          - 旧 run の workflow_notify / workflow_idem を引き継ぐ（待機中のワークフローが
            再開されなくなるのを防ぐ）。
        チャットの主用途が「レビュー中の帳票を、スキーマを直して取り直す」なので既定 True。
        REST の既定 False（needs_review も競合）は外部連携の二重投入防止で目的が違う。
        supersede_review=False はその REST 既定と同じ判定（has_active_run）になる。
        """
        if self._repo.get_document(tenant_id, document_id) is None:
            return {"ok": False, "message": "ドキュメントが見つかりません"}
        # REST /extract と同じ正規化・検証（敵対的レビュー確定の残穴）。LLM は空文字や
        # 実在しない schema_id を渡し得る。素通しすると extraction_runs の FK 違反で
        # 未捕捉 500 になる（REST 側で実際に起きたのと同じ経路）
        schema_id = (schema_id or "").strip() or None
        if schema_id is not None:
            problem = usable_schema_error(self._admin, tenant_id, schema_id)
            if problem is not None:
                return {"ok": False, "message": problem}

        inherited_options: dict[str, Any] = {}
        if supersede_review:
            if self._repo.has_processing_run(tenant_id, document_id):
                return {"ok": False, "message": "現在処理中です。完了を待ってください。"}
            latest = self._repo.get_latest_run(tenant_id, document_id)
            if latest is not None and latest.status in ("confirmed", "exported"):
                return {
                    "ok": False,
                    "message": "確定済みの結果があります。再抽出すると確定値が置き換わるため実行しません。",
                }
            if latest is not None:
                for key in ("workflow_notify", "workflow_idem"):
                    value = (latest.options or {}).get(key)
                    if value is not None:
                        inherited_options[key] = value
            # 新 run を作る前に旧 needs_review を終端させる
            self._repo.supersede_review_runs(tenant_id, document_id)
        elif self._repo.has_active_run(tenant_id, document_id):
            return {
                "ok": False,
                "message": "実行中または確認待ちの Run があります。完了か確定を待ってください。",
            }

        run_id, job_id = new_id("run"), new_id("job")
        self._repo.create_run(
            RunRecord(
                id=run_id,
                tenant_id=tenant_id,
                document_id=document_id,
                schema_id=schema_id,
                status="processing",
                options={**(options or {}), **inherited_options},
            )
        )
        self._repo.create_job(
            JobRecord(id=job_id, tenant_id=tenant_id, kind="extract", ref_id=run_id)
        )
        self._repo.set_document_status(tenant_id, document_id, "queued")
        self._queue.enqueue(
            "q.extract", {"job_id": job_id, "tenant_id": tenant_id, "run_id": run_id}
        )
        return {"ok": True, "job_id": job_id, "run_id": run_id}

    def update_schema(
        self, tenant_id: str, doc_type: str, field: dict[str, Any]
    ) -> dict[str, Any]:
        cur = self._admin.get_schema(tenant_id, doc_type)
        fields = list(cur.fields) if cur else []
        if any(f.name == field.get("name") for f in fields):
            return {"ok": False, "message": "同名の項目が既に存在します。"}
        # LLM は任意の名前を渡し得る。型違いは SchemaFieldDef の validator が、予約名
        # （__pages__ / __region__ / 先頭 __。設計 region-field-add-and-hint-v2 D9）は
        # put_schema が落とす。例外のまま返すとグラフごと落ちて会話が途切れるので、
        # ok=False で返して LLM が言い直せるようにする。
        try:
            fields.append(SchemaFieldDef(**field))
        except ValidationError as exc:
            return {"ok": False, "message": field_validation_message(exc)}
        try:
            rec = self._admin.put_schema(tenant_id, doc_type, fields)
        except ValueError as exc:
            # 予約名（D9）は put_schema（書き込み側の共通入口）が ValueError で拒む
            return {"ok": False, "message": str(exc)}
        return {"ok": True, "doc_type": rec.doc_type, "version": rec.version}

    def manage_rules(self, tenant_id: str, rule_id: str, status: str) -> dict[str, Any]:
        """ルールの有効化（active）/ 退役（retired）（§5.8.4）。

        語彙と有効化ゲートは PATCH /rules/{id} と同じ。以前は active/rejected/disabled
        を受けていたが、rejected / disabled はルールの状態語彙（draft / validating /
        active / retired）に存在せず、画面にも出ない値だった。
        """
        if status not in ("active", "retired"):
            return {"ok": False, "message": f"status は active / retired のみ: {status}"}
        rule = self._admin.get_rule(tenant_id, rule_id)
        if rule is None:
            return {"ok": False, "message": "ルールが見つかりません"}
        if status == "active" and not can_activate(rule):
            return {"ok": False, "message": ACTIVATION_BLOCKED_MESSAGE}
        rec = self._admin.set_rule_status(tenant_id, rule_id, status)
        if rec is None:  # 取得と更新の間で消えた等
            return {"ok": False, "message": "ルールの更新に失敗しました"}
        return {"ok": True, "rule_id": rec.id, "status": rec.status}
