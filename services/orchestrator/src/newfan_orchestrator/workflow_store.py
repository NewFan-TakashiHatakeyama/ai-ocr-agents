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


class InMemoryWorkflowRunStore:
    """テスト用。runner のロジックを DB なしで検証する。"""

    def __init__(self) -> None:
        self.runs: dict[str, dict[str, Any]] = {}
        self.node_runs: dict[tuple[str, str], dict[str, Any]] = {}
        self.extract_runs: dict[str, dict[str, Any]] = {}  # idem_key → run
        self.enqueued: list[dict[str, Any]] = []
        self.webhooks: dict[str, tuple[str, str]] = {}
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


class PgWorkflowRunStore:
    """本番実装（runtime 依存: sqlalchemy + psycopg）。"""

    def __init__(self, dsn: str, *, enqueue: EnqueueFn) -> None:
        from sqlalchemy import create_engine

        self._engine = create_engine(dsn, future=True)
        self._enqueue = enqueue

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
                    "INSERT INTO extraction_runs (id, tenant_id, document_id, schema_id,"
                    " status, options) VALUES (:r,:t,:d,:s,'processing', CAST(:o AS jsonb))"
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
                    "SELECT d.doc_type FROM extraction_runs r JOIN documents d"
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
                    "SELECT config FROM connections WHERE tenant_id=:t AND id=:i"
                    " AND type='webhook' AND status IN ('active','tested')"
                ),
                {"t": tenant_id, "i": connection_id},
            ).first()
        if row is None:
            return None
        cfg = row[0] or {}
        url = cfg.get("url")
        return (url, cfg.get("secret", "")) if url else None

