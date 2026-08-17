# mypy: ignore-errors
"""ワークフロー実行の永続化ポート（§16 設計 v0.2 §3 / §6）。

workflow_runs / workflow_node_runs は**観測用の射影**。実行の真実は lg_wf スキーマの
LangGraph checkpoint にあり、API/UI/監査/リトライ判断はこちらの射影を見る（§3.1）。

Pg 実装はアプリロール（newfan_app）接続が前提。RLS（ENABLE+FORCE）下で
各トランザクション先頭に app.tenant_id を設定する（§7.3）。
"""

from __future__ import annotations

import json
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Optional, Protocol

EnqueueFn = Callable[[str, dict[str, Any]], None]

# ワークフローが起こした抽出 Run の冪等キー（extraction_runs.options 内）。
# resume の再実行境界（interrupt より前のコードは再実行される — 実測）に対し、
# 「同じノードの enqueue は 1 回だけ」をこのキーで担保する（§6.4）。
IDEM_KEY = "workflow_idem"
NOTIFY_KEY = "workflow_notify"


@dataclass
class LockedRun:
    """行ロック中の workflow_run。update はロックと同一トランザクションで行う

    （別トランザクションから UPDATE すると自分のロックで待ち合わせて固まる）。
    """

    workflow_run_id: str
    tenant_id: str
    workflow_id: str
    workflow_version: int
    document_id: Optional[str]
    status: str
    graph_json: dict[str, Any]
    _update: Callable[..., None] = field(repr=False, default=lambda **kw: None)

    def update(
        self,
        *,
        status: str,
        waiting: Optional[dict[str, Any]] = None,
        error: Optional[dict[str, Any]] = None,
        finished: bool = False,
    ) -> None:
        self._update(status=status, waiting=waiting, error=error, finished=finished)


@dataclass(frozen=True)
class DbConnectionInfo:
    """sink.db_write の接続情報（§16.5 / P6）。

    秘密は DB に置かない。secret_ref（Secrets Manager の参照）だけを持ち、
    解決（GetSecretValue）は runner 側の resolver が行う。
    """

    config: dict[str, Any]
    secret_ref: Optional[str]
    allowed_tables: list[str]


class WorkflowRunStore(Protocol):
    def lock_run(
        self, tenant_id: str, workflow_run_id: str
    ) -> AbstractContextManager[Optional[LockedRun]]: ...

    def node_run_start(
        self, tenant_id: str, workflow_run_id: str, node_id: str, node_type: str
    ) -> None: ...
    def node_run_finish(
        self,
        tenant_id: str,
        workflow_run_id: str,
        node_id: str,
        status: str,
        *,
        output: Optional[dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> None: ...

    def ensure_extract_run(
        self,
        tenant_id: str,
        document_id: str,
        schema_id: str,
        options: dict[str, Any],
        idem_key: str,
        notify: dict[str, Any],
    ) -> str: ...
    def load_extract_result(self, tenant_id: str, run_id: str) -> dict[str, Any]: ...
    def get_webhook_connection(
        self, tenant_id: str, connection_id: str
    ) -> Optional[tuple[str, str]]: ...
    def get_db_connection(
        self, tenant_id: str, connection_id: str
    ) -> Optional["DbConnectionInfo"]: ...
    def get_s3_connection(self, tenant_id: str, connection_id: str) -> Optional[str]: ...


class InMemoryWorkflowRunStore:
    """テスト用。runner のロジックを DB なしで検証する。"""

    def __init__(self) -> None:
        self.runs: dict[str, dict[str, Any]] = {}
        self.node_runs: dict[tuple[str, str], dict[str, Any]] = {}
        self.extract_runs: dict[str, dict[str, Any]] = {}  # idem_key → run
        self.enqueued: list[dict[str, Any]] = []
        self.webhooks: dict[str, tuple[str, str]] = {}
        self.db_connections: dict[str, DbConnectionInfo] = {}
        self.s3_connections: dict[str, str] = {}
        self.extract_results: dict[str, dict[str, Any]] = {}  # run_id → result
        self._locked: set[str] = set()
        self._seq = 0

    # --- seed ---
    def seed_run(
        self,
        workflow_run_id: str,
        *,
        tenant_id: str,
        workflow_id: str,
        graph_json: dict[str, Any],
        document_id: str = "doc_1",
        workflow_version: int = 1,
    ) -> None:
        self.runs[workflow_run_id] = {
            "tenant_id": tenant_id,
            "workflow_id": workflow_id,
            "workflow_version": workflow_version,
            "document_id": document_id,
            "graph_json": graph_json,
            "status": "running",
            "waiting": None,
            "error": None,
            "finished": False,
        }

    def seed_webhook(self, tenant_id: str, connection_id: str, url: str, secret: str) -> None:
        self.webhooks[f"{tenant_id}:{connection_id}"] = (url, secret)

    def seed_extract_result(self, run_id: str, result: dict[str, Any]) -> None:
        self.extract_results[run_id] = result

    # --- WorkflowRunStore ---
    @contextmanager
    def lock_run(self, tenant_id: str, workflow_run_id: str) -> Iterator[Optional[LockedRun]]:
        row = self.runs.get(workflow_run_id)
        if row is None or row["tenant_id"] != tenant_id or workflow_run_id in self._locked:
            yield None
            return
        self._locked.add(workflow_run_id)

        def _update(*, status, waiting, error, finished) -> None:
            row["status"] = status
            row["waiting"] = waiting
            if error is not None:
                row["error"] = error
            row["finished"] = row["finished"] or finished

        try:
            yield LockedRun(
                workflow_run_id=workflow_run_id,
                tenant_id=tenant_id,
                workflow_id=row["workflow_id"],
                workflow_version=row["workflow_version"],
                document_id=row["document_id"],
                status=row["status"],
                graph_json=row["graph_json"],
                _update=_update,
            )
        finally:
            self._locked.discard(workflow_run_id)

    def node_run_start(self, tenant_id, workflow_run_id, node_id, node_type) -> None:
        key = (workflow_run_id, node_id)
        rec = self.node_runs.setdefault(
            key, {"node_type": node_type, "status": "pending", "attempt": 0}
        )
        if rec["status"] != "running":
            rec["attempt"] += 1
        rec["status"] = "running"

    def node_run_finish(self, tenant_id, workflow_run_id, node_id, status, *, output=None, error=None) -> None:
        rec = self.node_runs[(workflow_run_id, node_id)]
        rec["status"] = status
        rec["output"] = output
        rec["error"] = error

    def ensure_extract_run(self, tenant_id, document_id, schema_id, options, idem_key, notify) -> str:
        if idem_key in self.extract_runs:
            return self.extract_runs[idem_key]["run_id"]
        self._seq += 1
        run_id = f"run_wf_{self._seq}"
        self.extract_runs[idem_key] = {
            "run_id": run_id,
            "document_id": document_id,
            "schema_id": schema_id,
        }
        self.enqueued.append(
            {"job": "extract", "run_id": run_id, "tenant_id": tenant_id, "notify": notify}
        )
        return run_id

    def load_extract_result(self, tenant_id, run_id) -> dict[str, Any]:
        return dict(self.extract_results.get(run_id, {}))

    def get_webhook_connection(self, tenant_id, connection_id):
        return self.webhooks.get(f"{tenant_id}:{connection_id}")

    def seed_db_connection(
        self, tenant_id, connection_id, *, config=None, secret_ref=None, allowed_tables=()
    ) -> None:
        self.db_connections[f"{tenant_id}:{connection_id}"] = DbConnectionInfo(
            config=dict(config or {}), secret_ref=secret_ref,
            allowed_tables=list(allowed_tables),
        )

    def get_db_connection(self, tenant_id, connection_id):
        return self.db_connections.get(f"{tenant_id}:{connection_id}")

    def seed_s3_connection(self, tenant_id, connection_id, bucket) -> None:
        self.s3_connections[f"{tenant_id}:{connection_id}"] = bucket

    def get_s3_connection(self, tenant_id, connection_id):
        return self.s3_connections.get(f"{tenant_id}:{connection_id}")


class PgWorkflowRunStore:
    """本番実装（runtime 依存: sqlalchemy + psycopg）。"""

    def __init__(
        self,
        dsn: str,
        *,
        enqueue: EnqueueFn,
        resolve_secret: Optional[Callable[[str], str]] = None,
    ) -> None:
        from sqlalchemy import create_engine

        self._engine = create_engine(dsn, future=True)
        self._enqueue = enqueue
        # secret_ref（Secrets Manager 参照）の解決。未配線なら旧 config.secret のみ使える
        self._resolve_secret = resolve_secret

    def _rls(self, c, tenant_id: str) -> None:
        from sqlalchemy import text

        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})

    @contextmanager
    def lock_run(self, tenant_id: str, workflow_run_id: str) -> Iterator[Optional[LockedRun]]:
        from sqlalchemy import text

        with self._engine.begin() as c:
            self._rls(c, tenant_id)
            row = c.execute(
                text(
                    # FOR **NO KEY** UPDATE であること。FOR UPDATE にすると、ロック保持中の
                    # workflow_node_runs INSERT（別トランザクション）が FK 検査で参照行の
                    # FOR KEY SHARE を取れず、自分のロックを待って**永久に固まる**
                    # （実 AWS で最初の E2E がこれで停止した）。NO KEY UPDATE は
                    # KEY SHARE と競合せず、runner 同士の相互排他はそのまま保たれる。
                    "SELECT id, workflow_id, workflow_version, document_id, trigger, status"
                    " FROM workflow_runs WHERE tenant_id=:t AND id=:r"
                    " FOR NO KEY UPDATE SKIP LOCKED"
                ),
                {"t": tenant_id, "r": workflow_run_id},
            ).mappings().first()
            if row is None:
                # 存在しない or 他 runner がロック中。呼び手は ack しない（再配信待ち）
                yield None
                return

            def _update(*, status, waiting, error, finished) -> None:
                c.execute(
                    text(
                        "UPDATE workflow_runs SET status=:s,"
                        " state = state || CAST(:patch AS jsonb),"
                        " error = COALESCE(CAST(:err AS jsonb), error),"
                        " finished_at = CASE WHEN :fin THEN now() ELSE finished_at END"
                        " WHERE tenant_id=:t AND id=:r"
                    ),
                    {
                        "s": status,
                        "patch": json.dumps({"waiting": waiting}, ensure_ascii=False),
                        "err": json.dumps(error, ensure_ascii=False) if error else None,
                        "fin": finished,
                        "t": tenant_id,
                        "r": workflow_run_id,
                    },
                )

            yield LockedRun(
                workflow_run_id=workflow_run_id,
                tenant_id=tenant_id,
                workflow_id=row["workflow_id"],
                workflow_version=row["workflow_version"],
                document_id=row["document_id"],
                status=row["status"],
                graph_json=(row["trigger"] or {}).get("graph_json") or {},
                _update=_update,
            )

    def node_run_start(self, tenant_id, workflow_run_id, node_id, node_type) -> None:
        from sqlalchemy import text

        import uuid

        with self._engine.begin() as c:
            self._rls(c, tenant_id)
            c.execute(
                text(
                    "INSERT INTO workflow_node_runs"
                    " (id, tenant_id, workflow_run_id, node_id, node_type, status, attempt, started_at)"
                    " VALUES (:i,:t,:r,:n,:ty,'running',1, now())"
                    " ON CONFLICT (workflow_run_id, node_id) DO UPDATE SET"
                    "  status='running', started_at=now(),"
                    # interrupt からの resume 再実行（実測済みの再実行境界）では attempt を
                    # 増やさない。増えるのは失敗後の再試行だけにし、リトライ回数を意味のある
                    # 数字に保つ。
                    "  attempt = workflow_node_runs.attempt +"
                    "    CASE WHEN workflow_node_runs.status='running' THEN 0 ELSE 1 END"
                ),
                {
                    "i": f"wfn_{uuid.uuid4().hex[:24]}",
                    "t": tenant_id,
                    "r": workflow_run_id,
                    "n": node_id,
                    "ty": node_type,
                },
            )

    def node_run_finish(self, tenant_id, workflow_run_id, node_id, status, *, output=None, error=None) -> None:
        from sqlalchemy import text

        with self._engine.begin() as c:
            self._rls(c, tenant_id)
            c.execute(
                text(
                    "UPDATE workflow_node_runs SET status=:s,"
                    " output = CAST(:o AS jsonb), error = CAST(:e AS jsonb), finished_at=now()"
                    " WHERE tenant_id=:t AND workflow_run_id=:r AND node_id=:n"
                ),
                {
                    "s": status,
                    "o": json.dumps(output, ensure_ascii=False) if output is not None else None,
                    "e": json.dumps({"message": error}, ensure_ascii=False) if error else None,
                    "t": tenant_id,
                    "r": workflow_run_id,
                    "n": node_id,
                },
            )

    def ensure_extract_run(self, tenant_id, document_id, schema_id, options, idem_key, notify) -> str:
        from sqlalchemy import text

        import uuid

        with self._engine.begin() as c:
            self._rls(c, tenant_id)
            row = c.execute(
                text(
                    "SELECT id FROM extraction_runs WHERE tenant_id=:t AND document_id=:d"
                    f" AND options->>'{IDEM_KEY}' = :k"
                ),
                {"t": tenant_id, "d": document_id, "k": idem_key},
            ).first()
            if row is not None:
                return str(row[0])

            run_id = f"run_{uuid.uuid4().hex[:24]}"
            job_id = f"job_{uuid.uuid4().hex[:24]}"
            c.execute(
                text(
                    # engine_versions は NOT NULL で DEFAULT が無い（DDL）。gateway 経由の
                    # 抽出は ORM 側の default が埋めるが、直接 INSERT では明示が要る
                    # （省略すると NotNullViolation。実 AWS の E2E で検出）。実際の版は
                    # ワーカーが実行時に確定するため、ここでは空で作る。
                    "INSERT INTO extraction_runs (id, tenant_id, document_id, schema_id,"
                    " status, engine_versions, options)"
                    " VALUES (:r,:t,:d,:s,'processing','{}'::jsonb, CAST(:o AS jsonb))"
                ),
                {
                    "r": run_id,
                    "t": tenant_id,
                    "d": document_id,
                    "s": schema_id,
                    "o": json.dumps(
                        {**options, IDEM_KEY: idem_key, NOTIFY_KEY: notify}, ensure_ascii=False
                    ),
                },
            )
            c.execute(
                text(
                    "INSERT INTO jobs (id, tenant_id, kind, ref_id) VALUES (:j,:t,'extract',:r)"
                ),
                {"j": job_id, "t": tenant_id, "r": run_id},
            )
            c.execute(
                text("UPDATE documents SET status='queued', updated_at=now() WHERE id=:d"),
                {"d": document_id},
            )
        # enqueue はコミット後（コミット前に積むと、worker が run を読めない瞬間ができる）
        self._enqueue(
            "q.extract",
            {"job_id": job_id, "tenant_id": tenant_id, "run_id": run_id, "notify": notify},
        )
        return run_id

    def load_extract_result(self, tenant_id, run_id) -> dict[str, Any]:
        from sqlalchemy import text

        with self._engine.begin() as c:
            self._rls(c, tenant_id)
            head = c.execute(
                text(
                    "SELECT d.doc_type, d.original_name FROM extraction_runs r JOIN documents d"
                    " ON d.id = r.document_id WHERE r.tenant_id=:t AND r.id=:r"
                ),
                {"t": tenant_id, "r": run_id},
            ).first()
            rows = c.execute(
                text(
                    "SELECT field_name, COALESCE(final_value, value_normalized), confidence"
                    " FROM extraction_fields WHERE tenant_id=:t AND run_id=:r"
                ),
                {"t": tenant_id, "r": run_id},
            ).all()
        fields = {r[0]: {"value": r[1], "confidence": float(r[2] or 0.0)} for r in rows}
        confs = [f["confidence"] for f in fields.values()]
        return {
            "doc_type": head[0] if head else None,
            "original_name": head[1] if head else None,
            "fields": fields,
            # run 全体の確度は最弱フィールドで代表する（保守的。値が無ければ None → 条件 False）
            "run_confidence": min(confs) if confs else None,
        }

    def get_webhook_connection(self, tenant_id, connection_id):
        from sqlalchemy import text

        with self._engine.begin() as c:
            self._rls(c, tenant_id)
            row = c.execute(
                text(
                    "SELECT config, secret_ref FROM connections WHERE tenant_id=:t AND id=:i"
                    " AND type='webhook' AND status IN ('active','tested')"
                ),
                {"t": tenant_id, "i": connection_id},
            ).first()
        if row is None:
            return None
        cfg = row[0] or {}
        url = cfg.get("url")
        if not url:
            return None
        # P6 以降の登録は secret_ref（Secrets Manager）。旧行は config.secret に平文が
        # 残っているため fallback で読む（移行期間の互換。§16.5 / 旧 Q5）。
        # secret_ref があるのに resolver 未配線は設定ミス＝黙って空鍵にしない
        if row[1]:
            if self._resolve_secret is None:
                raise RuntimeError(
                    f"secret_ref があるのに resolver が配線されていません: {row[1]}"
                )
            return (url, self._resolve_secret(row[1]))
        return (url, cfg.get("secret", ""))

    def get_db_connection(self, tenant_id, connection_id):
        from sqlalchemy import text

        with self._engine.begin() as c:
            self._rls(c, tenant_id)
            row = c.execute(
                text(
                    "SELECT config, secret_ref, allowed_tables FROM connections"
                    " WHERE tenant_id=:t AND id=:i"
                    " AND type='postgres' AND status IN ('active','tested')"
                ),
                {"t": tenant_id, "i": connection_id},
            ).first()
        if row is None:
            return None
        return DbConnectionInfo(
            config=row[0] or {}, secret_ref=row[1], allowed_tables=list(row[2] or []),
        )

    def get_s3_connection(self, tenant_id, connection_id):
        from sqlalchemy import text

        with self._engine.begin() as c:
            self._rls(c, tenant_id)
            row = c.execute(
                text(
                    "SELECT config->>'bucket' FROM connections WHERE tenant_id=:t AND id=:i"
                    " AND type='s3' AND status IN ('active','tested')"
                ),
                {"t": tenant_id, "i": connection_id},
            ).first()
        return row[0] if row else None



class PgTriggerStore:
    """TriggerStore の PostgreSQL 実装（§16 設計 v0.2 §7.2 / P4）。

    アプリロール接続・RLS 前提。claim（source_cursors）・documents・workflow_runs は
    **同一トランザクション**で行う。分けると「claim 済みなのに未取込」の孤児ができ、
    DD-13 が再配信をすべて弾いて取り込まれなくなる。
    """

    def __init__(self, dsn: str) -> None:
        from sqlalchemy import create_engine

        self._engine = create_engine(dsn, future=True)

    def _rls(self, c, tenant_id: str) -> None:
        from sqlalchemy import text

        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})

    def list_active_workflows(self, tenant_id: str):
        from sqlalchemy import text

        with self._engine.begin() as c:
            self._rls(c, tenant_id)
            rows = c.execute(
                text(
                    "SELECT id, version, graph_json FROM workflows"
                    " WHERE tenant_id=:t AND status='active'"
                ),
                {"t": tenant_id},
            ).all()
        return [(r[0], r[1], r[2] or {}) for r in rows]

    def get_s3_connection_bucket(self, tenant_id: str, connection_id: str):
        from sqlalchemy import text

        with self._engine.begin() as c:
            self._rls(c, tenant_id)
            row = c.execute(
                text(
                    "SELECT config->>'bucket' FROM connections WHERE tenant_id=:t AND id=:i"
                    " AND type='s3' AND status IN ('active','tested')"
                ),
                {"t": tenant_id, "i": connection_id},
            ).first()
        return row[0] if row else None

    def get_folder_connection(self, tenant_id: str, connection_id: str, conn_type: str):
        from sqlalchemy import text

        from newfan_orchestrator.workflow_trigger import FolderConnection

        # ソース接続は取込のみでデータ流出面が無く、疎通テスト（test_connection）も
        # postgres 専用のため、untested を弾かない（疎通は同期の成否で確認する）。
        # disabled だけは尊重する。gdrive/m365/box で同型（フォルダ監視系）。
        with self._engine.begin() as c:
            self._rls(c, tenant_id)
            row = c.execute(
                text(
                    "SELECT config->>'folder_id', secret_ref FROM connections"
                    " WHERE tenant_id=:t AND id=:i AND type=:ty"
                    " AND status <> 'disabled'"
                ),
                {"t": tenant_id, "i": connection_id, "ty": conn_type},
            ).first()
        if row is None or not row[0]:
            return None
        return FolderConnection(folder_id=row[0], secret_ref=row[1])

    def mark_connection_tested(self, tenant_id: str, connection_id: str) -> None:
        from sqlalchemy import text

        # untested からのみ昇格（active を巻き戻さない）。同期成功＝疎通OK の記録
        with self._engine.begin() as c:
            self._rls(c, tenant_id)
            c.execute(
                text(
                    "UPDATE connections SET status='tested'"
                    " WHERE tenant_id=:t AND id=:i AND status='untested'"
                ),
                {"t": tenant_id, "i": connection_id},
            )

    def already_claimed(self, tenant_id, connection_id, source_key, content_hash) -> bool:
        from sqlalchemy import text

        with self._engine.begin() as c:
            self._rls(c, tenant_id)
            row = c.execute(
                text(
                    "SELECT 1 FROM source_cursors WHERE tenant_id=:t AND connection_id=:c"
                    " AND source_key=:k AND content_hash=:h"
                ),
                {"t": tenant_id, "c": connection_id, "k": source_key, "h": content_hash},
            ).first()
        return row is not None

    def list_tenant_ids(self):
        from sqlalchemy import text

        # tenants は RLS 対象外（tenant_id 列を持たない台帳）。schedule はテナント横断で
        # 評価する必要があり、ここだけが横断的に読む
        with self._engine.begin() as c:
            rows = c.execute(text("SELECT id FROM tenants")).all()
        return [r[0] for r in rows]

    def register_scheduled_run(
        self, tenant_id, *, workflow_id, workflow_version, graph_json, node_id, fire_minute
    ):
        """schedule 発火の run を作る。分単位の dedup は source_cursors の UNIQUE
        （connection_id=node_id / source_key=schedule:workflow / content_hash=分時刻）。
        claim と run INSERT は同一 TX（片方だけ残ると発火漏れ/二重になる）。"""
        import uuid

        from sqlalchemy import text

        with self._engine.begin() as c:
            self._rls(c, tenant_id)
            r = c.execute(
                text(
                    "INSERT INTO source_cursors"
                    " (id, tenant_id, connection_id, source_key, content_hash, workflow_id)"
                    " VALUES (:i,:t,:c,:k,:h,:w)"
                    " ON CONFLICT (tenant_id, connection_id, source_key, content_hash)"
                    " DO NOTHING"
                ),
                {
                    "i": f"cur_{uuid.uuid4().hex[:24]}",
                    "t": tenant_id,
                    "c": node_id,
                    "k": f"schedule:{workflow_id}",
                    "h": fire_minute,
                    "w": workflow_id,
                },
            )
            if not r.rowcount:
                return None  # 既に発火済み（別 consumer / 再評価）
            run_id = f"wfrun_{uuid.uuid4().hex[:24]}"
            c.execute(
                text(
                    "INSERT INTO workflow_runs (id, tenant_id, workflow_id,"
                    " workflow_version, trigger, document_id, state, status)"
                    " VALUES (:i,:t,:w,:v, CAST(:tr AS jsonb), NULL, '{}'::jsonb, 'running')"
                ),
                {
                    "i": run_id,
                    "t": tenant_id,
                    "w": workflow_id,
                    "v": workflow_version,
                    "tr": json.dumps(
                        {
                            "type": "schedule",
                            "node_id": node_id,
                            "fired_at": fire_minute,
                            "graph_json": graph_json,
                        },
                        ensure_ascii=False,
                    ),
                },
            )
        return run_id

    def register_ingested(
        self, tenant_id, *, source_key, content_hash, document, pages, matches,
        trigger_type="s3_event",
    ):
        import uuid

        from sqlalchemy import text

        with self._engine.begin() as c:
            self._rls(c, tenant_id)
            claimed: set[str] = set()
            for connection_id in {m.connection_id for m in matches}:
                r = c.execute(
                    text(
                        "INSERT INTO source_cursors"
                        " (id, tenant_id, connection_id, source_key, content_hash, workflow_id)"
                        " VALUES (:i,:t,:c,:k,:h,:w)"
                        " ON CONFLICT (tenant_id, connection_id, source_key, content_hash)"
                        " DO NOTHING"
                    ),
                    {
                        "i": f"cur_{uuid.uuid4().hex[:24]}",
                        "t": tenant_id,
                        "c": connection_id,
                        "k": source_key,
                        "h": content_hash,
                        "w": next(
                            m.workflow_id for m in matches if m.connection_id == connection_id
                        ),
                    },
                )
                if r.rowcount:
                    claimed.add(connection_id)
            if not claimed:
                return []  # 他プロセスが取込済み（SQS at-least-once の競合）

            c.execute(
                text(
                    "INSERT INTO documents (id, tenant_id, storage_uri, original_name,"
                    " mime_type, page_count, external_ref, status)"
                    " VALUES (:i,:t,:u,:n,:m,:p,:e,'uploaded')"
                ),
                {
                    "i": document["id"],
                    "t": tenant_id,
                    "u": document["storage_uri"],
                    "n": document.get("original_name"),
                    "m": document["mime_type"],
                    "p": document.get("page_count"),
                    "e": document.get("external_ref"),
                },
            )
            for p in pages:
                c.execute(
                    text(
                        "INSERT INTO pages (id, tenant_id, document_id, page_no, width,"
                        " height, image_uri, preproc)"
                        " VALUES (:i,:t,:d,:n,:w,:h,:u, CAST(:pp AS jsonb))"
                    ),
                    {
                        "i": f"{document['id']}:{p['page_no']}",
                        "t": tenant_id,
                        "d": document["id"],
                        "n": p["page_no"],
                        "w": p.get("width"),
                        "h": p.get("height"),
                        "u": p["image_uri"],
                        "pp": json.dumps(p.get("preproc") or {}, ensure_ascii=False),
                    },
                )

            run_ids: list[str] = []
            for m in matches:
                if m.connection_id not in claimed:
                    continue
                run_id = f"wfrun_{uuid.uuid4().hex[:24]}"
                c.execute(
                    text(
                        "INSERT INTO workflow_runs (id, tenant_id, workflow_id,"
                        " workflow_version, trigger, document_id, state, status)"
                        " VALUES (:i,:t,:w,:v, CAST(:tr AS jsonb), :d, '{}'::jsonb, 'running')"
                    ),
                    {
                        "i": run_id,
                        "t": tenant_id,
                        "w": m.workflow_id,
                        "v": m.workflow_version,
                        "tr": json.dumps(
                            {
                                # 取込元の実種別（s3_event / gdrive_event）。監査で
                                # 出所を誤認しないよう呼び出し元が渡す（レビュー確定）
                                "type": trigger_type,
                                "source_key": source_key,
                                "etag": content_hash,
                                "node_id": m.node_id,
                                # 版の固定（§11.1）。手動実行と同じくスナップショットを持つ
                                "graph_json": m.graph_json,
                            },
                            ensure_ascii=False,
                        ),
                        "d": document["id"],
                    },
                )
                run_ids.append(run_id)
        return run_ids
