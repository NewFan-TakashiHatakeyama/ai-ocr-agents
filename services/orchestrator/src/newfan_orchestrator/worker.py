"""抽出ワーカー（§2.1 orchestrator-svc / §9）。

キューから extract/resume ジョブを消費し、抽出グラフ（interrupt/resume 対応）を invoke する。
- extract: グラフ実行。needs_review で interrupt 停止 → snapshot を needs_review 保存＋webhook。
  自動確定なら finalize が confirmed 保存＋q.export enqueue まで実施済み。
- resume: Command(resume=feedback) で hitl_review 以降を継続 → finalize（confirmed）。

checkpointer 付きの graph（build_graph(checkpointer=..., context_store=...)）が前提。
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from newfan_orchestrator.consumer import QueueConsumer
from newfan_orchestrator.persistence import ContextStore

WebhookFn = Callable[[str, dict[str, Any]], None]


class ExtractionWorker:
    def __init__(
        self,
        graph: Any,
        store: ContextStore,
        consumer: QueueConsumer,
        *,
        webhook: Optional[WebhookFn] = None,
    ) -> None:
        self._graph = graph
        self._store = store
        self._consumer = consumer
        self._webhook = webhook

    def process(self, payload: dict[str, Any]) -> str:
        """1 ジョブを処理して最終 status を返す（'confirmed' / 'needs_review'）。"""
        run_id = payload["run_id"]
        tenant_id = payload["tenant_id"]
        config = {"configurable": {"thread_id": run_id}}

        if "resume" in payload:  # 再開ジョブ（feedback は None/空でも可＝上書きなし確定）
            from langgraph.types import Command  # 遅延 import

            self._graph.invoke(Command(resume=payload.get("resume") or {}), config)
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
            )
            if self._webhook is not None:
                self._webhook(
                    "document.needs_review",
                    {"run_id": run_id, "tenant_id": tenant_id, "document_id": state.get("document_id", "")},
                )
            return "needs_review"
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
            except Exception:  # noqa: BLE001 - 失敗ジョブは ACK せず再配信に委ねる（§9）
                continue
        return processed
