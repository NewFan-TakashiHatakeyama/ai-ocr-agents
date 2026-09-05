"""抽出ワーカー（§2.1 orchestrator-svc / §9）。

キューから extract/resume ジョブを消費し、抽出グラフ（interrupt/resume 対応）を invoke する。
- extract: グラフ実行。needs_review で interrupt 停止 → snapshot を needs_review 保存＋webhook。
  自動確定なら finalize が confirmed 保存＋q.export enqueue まで実施済み。
- resume: Command(resume=feedback) で hitl_review 以降を継続 → finalize（confirmed）。

checkpointer 付きの graph（build_graph(checkpointer=..., context_store=...)）が前提。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional

from newfan_metrics import run_duration_seconds, tenant_scope

from newfan_orchestrator.consumer import QueueConsumer
from newfan_orchestrator.persistence import ContextStore

WebhookFn = Callable[[str, dict[str, Any]], None]

logger = logging.getLogger(__name__)


class RunGoneError(Exception):
    """処理対象の run（または帳票）が既に無い。再配信しても永久に直らない。

    帳票の削除（DELETE /v1/documents/{id}）が in-flight のジョブと競合すると起きる。
    通常の失敗と違って再試行に意味が無いので、ジョブを dead 終端にして ACK する。
    """


class ExtractionWorker:
    def __init__(
        self,
        graph: Any,
        store: ContextStore,
        consumer: QueueConsumer,
        *,
        webhook: Optional[WebhookFn] = None,
        enqueue: Optional[Callable[[str, dict[str, Any]], None]] = None,
        usage_snapshot: Optional[Callable[[], dict[str, float]]] = None,
    ) -> None:
        self._graph = graph
        self._store = store
        self._consumer = consumer
        # LLM 使用量の断面（LLMAdapter.usage_snapshot）。run 前後の差分を
        # extraction_runs.metrics へ永続化する（SCR-04 の LLM コスト, §12.1）
        self._usage_snapshot = usage_snapshot
        self._webhook = webhook
        self._enqueue = enqueue

    def _notify(self, payload: dict[str, Any], status: str) -> None:
        """ジョブ payload に notify 先が書かれていたら、完了をそこへ積む（§16 設計 v0.2 §6.3）。

        ワークフロー層はこれで抽出の完了を受けて resume する。ワーカーは
        「指定された stream に完了を書く」ことしか知らず、ワークフローの概念は
        持ち込まない（DD-11）。顧客向け webhook（§6.4、失敗や再送がある）には
        内部制御を乗せない。
        """
        notify = payload.get("notify")
        if not notify or self._enqueue is None:
            return
        try:
            self._enqueue(
                notify["stream"],
                {
                    "type": "resume",
                    "tenant_id": payload["tenant_id"],
                    "workflow_run_id": notify["workflow_run_id"],
                    "event": {
                        "kind": "confirm_done" if "resume" in payload else "extract_done",
                        "run_id": payload["run_id"],
                        "status": status,
                    },
                },
            )
        except Exception:  # noqa: BLE001 - notify の失敗で抽出そのものを落とさない
            logger.exception(
                "[worker] 完了 notify の enqueue に失敗: run_id=%s", payload.get("run_id")
            )

    def _job(self, payload: dict[str, Any], status: str, error_code: Optional[str] = None) -> None:
        """jobs.status を進める。§6.3 の API はクライアントが GET /jobs/{id} を polling して
        終了を待つ契約で、ここを更新しないと成功しても queued のまま見え続ける。
        job_id の無い payload（旧形式・テスト）でも落とさない。"""
        job_id = payload.get("job_id")
        if job_id is None:
            return
        try:
            self._store.set_job_status(payload["tenant_id"], job_id, status, error_code=error_code)
        except Exception:  # noqa: BLE001 - 状態表示の失敗で抽出そのものを落とさない
            logger.exception("[worker] jobs.status の更新に失敗: job_id=%s status=%s", job_id, status)

    def process(self, payload: dict[str, Any]) -> str:
        """1 ジョブを処理して最終 status を返す（'confirmed' / 'needs_review'）。"""
        # §12.1 の {tenant} ラベル用。ここで一度張れば、LLMAdapter のような
        # tenant_id を受け取らない低レイヤの計装も正しいラベルを付けられる。
        with tenant_scope(payload.get("tenant_id", "")):
            started = time.monotonic()
            before = self._usage_snapshot() if self._usage_snapshot else None
            outcome = "failed"
            try:
                outcome = self._process(payload)
                return outcome
            finally:
                run_duration_seconds.labels(outcome=outcome).observe(time.monotonic() - started)
                if before is not None:
                    try:
                        after = self._usage_snapshot()  # type: ignore[misc]
                        delta = {
                            "llm_input_tokens": after["input_tokens"] - before["input_tokens"],
                            "llm_output_tokens": after["output_tokens"] - before["output_tokens"],
                            "llm_cost_jpy": after["cost_jpy"] - before["cost_jpy"],
                        }
                        if any(v > 0 for v in delta.values()):
                            self._store.add_run_metrics(
                                payload["tenant_id"], payload["run_id"], delta
                            )
                    except Exception:  # noqa: BLE001 - 計測はジョブを落とさない
                        logger.warning("[worker] LLM 使用量の記録に失敗", exc_info=True)

    def _process(self, payload: dict[str, Any]) -> str:
        run_id = payload["run_id"]
        tenant_id = payload["tenant_id"]
        config = {"configurable": {"thread_id": run_id}}

        # 帳票が消えていたら何もせず降りる。resume 分岐より前に見るのが要点で、
        # Command(resume=...) は checkpointer だけを見て走るため、DB に run が
        # 無くても finalize まで進んでしまう（そこで FK 違反 → 無限再配信）。
        if not self._store.run_exists(tenant_id, run_id):
            raise RunGoneError(run_id)

        self._job(payload, "running")

        if "resume" in payload:  # 再開ジョブ（feedback は None/空でも可＝上書きなし確定）
            from langgraph.types import Command  # 遅延 import

            self._graph.invoke(Command(resume=payload.get("resume") or {}), config)
            self._job(payload, "succeeded")
            self._notify(payload, "confirmed")
            return "confirmed"

        self._graph.invoke({"run_id": run_id, "tenant_id": tenant_id}, config)
        snapshot = self._graph.get_state(config)
        if snapshot.next:  # hitl_review で interrupt 停止（未完了ノードあり）
            state = snapshot.values
            self._store.save_result(
                tenant_id,
                run_id,
                fields=state.get("fields", []),
                tables=state.get("tables", []),
                review_items=state.get("review_items", []),
                status="needs_review",
                fallback_pages=state.get("fallback_pages", []),
            )
            if self._webhook is not None:
                self._webhook(
                    "document.needs_review",
                    {"run_id": run_id, "tenant_id": tenant_id, "document_id": state.get("document_id", "")},
                )
            # ジョブとしては成功。needs_review は「人の確認待ち」であって失敗ではない
            # （§4.4 の interrupt。ここを failed にすると再配信されて二重実行になる）。
            self._job(payload, "succeeded")
            self._notify(payload, "needs_review")
            return "needs_review"
        self._job(payload, "succeeded")
        self._notify(payload, "confirmed")
        return "confirmed"  # finalize が confirmed 保存＋q.export enqueue 済み

    def run_once(self, *, count: int = 10) -> int:
        """現在キューにあるジョブをまとめて処理する（テスト・バッチ用）。失敗は ACK せず残す（§9 再配信）。"""
        messages = self._consumer.consume(count=count)
        processed = 0
        for message_id, payload in messages:
            try:
                self.process(payload)
                self._consumer.ack(message_id)
                processed += 1
            except RunGoneError:
                # 参照先が消えている＝再試行しても永久に直らない。ACK しないと
                # xautoclaim が 60 秒ごとに拾い続ける（試行上限が無い）。
                logger.warning(
                    "[worker] 対象の run が存在しないためジョブを破棄: message_id=%s run_id=%s",
                    message_id,
                    payload.get("run_id"),
                )
                self._job(payload, "dead", "E1001")
                self._consumer.ack(message_id)
                continue
            except Exception:  # noqa: BLE001 - 失敗ジョブは ACK せず再配信に委ねる（§9）
                # 握り潰すと本番で原因究明できない（pending が増えるだけで無言になる）。
                # ACK しない方針は維持しつつ、必ずスタックトレースを残す。
                logger.exception(
                    "[worker] ジョブ処理に失敗（ACK せず再配信に委ねる）: message_id=%s run_id=%s",
                    message_id,
                    payload.get("run_id"),
                )
                # 再配信で running に戻るので failed のままにはならない。ここを書かないと
                # 落ちたジョブが queued に見え、クライアントは待ち続ける。
                self._job(payload, "failed", "E9001")
                # extract ジョブが落ちると run は processing のまま残り、has_active_run が
                # 掴んで再抽出が E1005 で塞がる（帳票が行き止まりになる）。processing 限定で
                # failed へ落とす（再配信が成功すれば save_result が needs_review で上書き）。
                run_id = payload.get("run_id")
                if run_id:
                    try:
                        self._store.mark_run_failed(payload["tenant_id"], run_id)
                    except Exception:  # noqa: BLE001
                        logger.exception("[worker] run の failed 化に失敗: run_id=%s", run_id)
                continue
        return processed
