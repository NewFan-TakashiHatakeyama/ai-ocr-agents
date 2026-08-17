# mypy: ignore-errors
"""workflow-runner（§16 設計 v0.2 §6）。q.workflow を消費して LangGraph を invoke/resume する。

1 つの workflow_run は interrupt で区切られたセグメントの列として実行される（§2.2）。
セグメント実行中だけ行ロック（FOR UPDATE SKIP LOCKED）と DB 接続を使い、待機中は
プロセスも接続も持たない。実行の真実は lg_wf スキーマの checkpoint、
workflow_runs/_node_runs は観測用の射影（§3.1）。

メッセージ（§6.3）:
  {"type": "start",  "tenant_id", "workflow_run_id"}
  {"type": "resume", "tenant_id", "workflow_run_id", "event": {"kind", "run_id", "status"}}
  {"type": "retry",  "tenant_id", "workflow_run_id"}
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from newfan_metrics import tenant_scope, workflow_runs_total
from newfan_workflow import WorkflowGraph

from newfan_orchestrator.consumer import QueueConsumer
from newfan_orchestrator.workflow_graph import (
    KIND_AWAIT_HITL,
    RunnerDeps,
    build_workflow_app,
)
from newfan_orchestrator.workflow_store import LockedRun, WorkflowRunStore

logger = logging.getLogger(__name__)


class NotReady(RuntimeError):
    """今は処理できない（行ロック中 / checkpoint 未確定の resume 先行など）。

    ack せず再配信に委ねる（§9）。失敗ではないので failed にもしない。
    """


class WorkflowRunner:
    def __init__(
        self,
        store: WorkflowRunStore,
        consumer: QueueConsumer,
        deps: RunnerDeps,
        *,
        checkpointer: Any = None,
    ) -> None:
        self._store = store
        self._consumer = consumer
        self._deps = deps
        self._checkpointer = checkpointer
        # (workflow_id, version) → compiled graph。版は固定（§11.1）なので無効化不要。
        # 構築コストは実測 0.82ms だが、キャッシュはタダなので持つ。
        self._cache: dict[tuple[str, int], Any] = {}

    # --- graph ---

    def _app(self, locked: LockedRun) -> Any:
        key = (locked.workflow_id, locked.workflow_version)
        if key not in self._cache:
            wf = WorkflowGraph.model_validate(locked.graph_json)
            self._cache[key] = build_workflow_app(
                wf, self._deps, checkpointer=self._checkpointer
            )
        return self._cache[key]

    # --- 1 メッセージの処理 ---

    def process(self, payload: dict[str, Any]) -> str:
        tenant_id = payload["tenant_id"]
        run_id = payload["workflow_run_id"]
        with tenant_scope(tenant_id), self._store.lock_run(tenant_id, run_id) as locked:
            if locked is None:
                raise NotReady(f"run が見つからないかロック中: {run_id}")
            typ0 = payload.get("type", "start")
            # 終端済み run への再配信は ack して捨てる（NotReady だと永久再配信、
            # 通すと射影とメトリクスが二重計上される）。retry だけは failed からの
            # 再実行が正規経路なので failed を除外する（§11.2）
            terminal = ("succeeded", "skipped") if typ0 == "retry" else (
                "succeeded", "failed", "skipped"
            )
            if typ0 in ("start", "resume", "retry") and locked.status in terminal:
                logger.info(
                    "[workflow] 終端済み run への %s を破棄: run=%s status=%s",
                    typ0, run_id, locked.status,
                )
                return "ignored"
            app = self._app(locked)
            config = {"configurable": {"thread_id": run_id}}
            typ = payload.get("type", "start")

            try:
                result = self._invoke(app, config, typ, payload, locked)
            except NotReady:
                raise
            except Exception as exc:  # noqa: BLE001 - ノード失敗は射影に記録して終端
                logger.exception(
                    "[workflow] セグメント実行に失敗: run=%s type=%s", run_id, typ
                )
                locked.update(
                    status="failed",
                    waiting=None,
                    error={"message": str(exc)[:2000]},
                    finished=True,
                )
                workflow_runs_total.labels(
                    workflow=locked.workflow_id, status="failed"
                ).inc()
                return "failed"

            return self._project(locked, result)

    def _invoke(
        self,
        app: Any,
        config: dict[str, Any],
        typ: str,
        payload: dict[str, Any],
        locked: LockedRun,
    ) -> Optional[dict[str, Any]]:
        from langgraph.types import Command

        if typ == "start":
            snapshot = app.get_state(config)
            if snapshot.next or (snapshot.values or {}):
                # start の再配信（ack 前クラッシュ）。invoke し直すと最初から走り直すため、
                # interrupt 待ちなら射影だけ合わせ、superstep 間で落ちていた場合
                # （checkpoint あり・interrupt なし・next あり）は checkpoint から続きを走らせる。
                logger.info("[workflow] start 重複（開始済み）: %s", locked.workflow_run_id)
                resumed = self._result_from_snapshot(snapshot)
                if resumed is not None and "__interrupt__" in resumed:
                    return resumed
                if snapshot.next:
                    return app.invoke(None, config)
                return resumed
            init: dict[str, Any] = {
                "workflow_run_id": locked.workflow_run_id,
                "tenant_id": locked.tenant_id,
                "document_id": locked.document_id or "",
                # 複数トリガーの WF で「発火した経路だけ」を実行するための入口情報。
                # 無い（旧run・旧形式）場合はグラフ側が全経路にフォールバックする
                "fired_trigger": locked.trigger_node_id,
                "node_outputs": {},
            }
            return app.invoke(init, config)

        if typ == "resume":
            snapshot = app.get_state(config)
            if not snapshot.next:
                if snapshot.values:
                    # checkpoint は完走済みなのに射影が終端でない（最終セグメント完了と
                    # 射影 commit の間のクラッシュ）。invoke するものが無いので射影を
                    # 終端へ合わせて ack する。NotReady にすると永久再配信になる
                    logger.info(
                        "[workflow] 完走済み checkpoint への resume（射影を終端へ同期): %s",
                        locked.workflow_run_id,
                    )
                    return self._result_from_snapshot(snapshot)
                # 抽出完了 notify が start の checkpoint 確定より先に届いた
                # （ack 前クラッシュのレース）。再配信で解消する。
                raise NotReady("interrupt 未確定の resume（再配信待ち）")
            event = dict(payload.get("event") or {})
            pending = self._result_from_snapshot(snapshot) or {}
            pending_kinds = {
                (getattr(i, "value", None) or {}).get("kind")
                for i in pending.get("__interrupt__", ())
            }
            if KIND_AWAIT_HITL in pending_kinds and event.get("kind") != "confirm_done":
                # 人手ゲート待ちの run に extract_done 等が再配信された（at-least-once）。
                # そのまま resume すると interrupt の戻り値になり、人手確認なしに
                # ゲートが開いて未確定値が下流へ流れる（レビューで検出）。
                # 射影だけ合わせて ack し、本物の confirm_done を待つ
                logger.info(
                    "[workflow] await_hitl 中の %s を破棄（confirm_done のみ受理): run=%s",
                    event.get("kind"), locked.workflow_run_id,
                )
                return pending
            if event.get("kind") in ("extract_done", "confirm_done") and event.get("run_id"):
                # fields は worker の notify に載せない（メッセージ肥大とレイヤ越えを避け、
                # 採点済みの正本 extraction_fields から読む）
                event.update(
                    self._store.load_extract_result(locked.tenant_id, event["run_id"])
                )
            return app.invoke(Command(resume=event), config)

        if typ == "retry":
            # 失敗セグメントの再実行。checkpoint から続きが走る。完了済みノードは
            # 再実行されない（実測済みの再実行境界）。
            return app.invoke(None, config)

        raise NotReady(f"未知のメッセージ type: {typ}")

    @staticmethod
    def _result_from_snapshot(snapshot: Any) -> Optional[dict[str, Any]]:
        values = dict(snapshot.values or {})
        interrupts = []
        for task in getattr(snapshot, "tasks", ()) or ():
            interrupts.extend(getattr(task, "interrupts", ()) or ())
        if interrupts:
            values["__interrupt__"] = tuple(interrupts)
        return values

    # --- 射影の更新（§3.1） ---

    def _project(self, locked: LockedRun, result: Optional[dict[str, Any]]) -> str:
        interrupts = (result or {}).get("__interrupt__") or ()
        if interrupts:
            value = getattr(interrupts[0], "value", None) or {}
            kind = value.get("kind", "")
            status = "waiting_hitl" if kind == KIND_AWAIT_HITL else "running"
            locked.update(status=status, waiting=dict(value), error=None)
            return status
        locked.update(status="succeeded", waiting=None, error=None, finished=True)
        workflow_runs_total.labels(workflow=locked.workflow_id, status="succeeded").inc()
        return "succeeded"

    # --- 消費ループ ---

    def run_once(self, *, count: int = 10) -> int:
        processed = 0
        for message_id, payload in self._consumer.consume(count=count):
            try:
                self.process(payload)
                self._consumer.ack(message_id)
                processed += 1
            except NotReady as exc:
                # ack しない（再配信待ち）。ログは残す（無言のスタックを作らない）
                logger.info("[workflow] 保留: %s", exc)
            except Exception:  # noqa: BLE001 - インフラ故障は ACK せず再配信（§9）
                logger.exception(
                    "[workflow] メッセージ処理に失敗（ACK せず再配信に委ねる): id=%s",
                    message_id,
                )
        return processed
