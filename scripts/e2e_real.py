"""実 PostgreSQL + Redis での E2E（§9 / §4.4）。

外部境界（GPU/LLM）のみ Fake、DB/キュー/チェックポイントは本番相当。
  Phase A（自動確定）: enqueue → worker → extraction_fields 保存 + status=confirmed。
  Phase B（HITL）    : 低信頼 → needs_review interrupt → 修正 resume ジョブ → confirmed。

実行:
    DATABASE_URL=postgresql+psycopg://newfan:newfan@localhost:5433/newfan \
    REDIS_URL=redis://localhost:6380 \
    uv run --with "psycopg[binary]" --with redis python scripts/e2e_real.py
"""

from __future__ import annotations

import json
import os
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from sqlalchemy import create_engine, text

from newfan_gateway.prod import QueueOrchestratorClient, RedisQueue as GwRedisQueue
from newfan_llm_adapter import FakeProvider, LLMAdapter, PromptBundle, default_bundle_dir
from newfan_memory import HashingEmbedder, InMemoryMemoryRepository, MemoryService
from newfan_orchestrator.graph import build_graph
from newfan_orchestrator.pg_persistence import PgContextStore
from newfan_orchestrator.redis_io import RedisQueue, RedisStreamConsumer
from newfan_orchestrator.worker import ExtractionWorker
from newfan_paddle_client import LayoutParsingResponse

DSN = os.environ["DATABASE_URL"]
REDIS = os.environ["REDIS_URL"]
TENANT = "ten_e2e"


def _layout(conf: float) -> dict[str, Any]:
    return {
        "layoutParsingResults": [
            {
                "prunedResult": {
                    "parsing_res_list": [
                        {"block_bbox": [40, 40, 520, 90], "block_label": "text", "block_content": "x", "block_id": 0, "block_order": 0}
                    ],
                    "overall_ocr_res": {"rec_texts": ["128000"], "rec_scores": [conf], "rec_polys": [[[300, 180], [430, 180], [430, 212], [300, 212]]]},
                },
                "markdown": {"text": "md", "isStart": True, "isEnd": True},
            }
        ]
    }


class _FakeStructure:
    def __init__(self, conf: float) -> None:
        self._conf = conf

    def layout_parsing(self, file_b64: str, *, file_type: int = 1) -> LayoutParsingResponse:
        return LayoutParsingResponse.model_validate(_layout(self._conf))


def _llm(system: str, user: str) -> str:
    if "校正" in system:  # llm_correct: 変更しない
        return json.dumps({"corrected": "128000", "changed": False, "needs_review": True, "used_pairs": [], "memory_refs": [], "rationale": "", "confidence": 0.5})
    return json.dumps({"fields": [{"name": "total_amount", "value": "128000", "span_ids": [0], "page": 1}], "tables": [], "unmapped_required": []})


def _seed(engine, doc: str, run: str, sch: str) -> None:  # type: ignore[no-untyped-def]
    with engine.begin() as c:
        c.execute(text("DELETE FROM documents WHERE id = :d"), {"d": doc})
        c.execute(text("DELETE FROM field_schemas WHERE id = :s"), {"s": sch})
        c.execute(text("INSERT INTO tenants (id, name) VALUES (:i,'demo') ON CONFLICT (id) DO NOTHING"), {"i": TENANT})
        c.execute(text("INSERT INTO documents (id, tenant_id, storage_uri, mime_type, page_count, doc_type, status) VALUES (:i,:t,'s3://x','image/png',1,'invoice','processing')"), {"i": doc, "t": TENANT})
        c.execute(text("INSERT INTO pages (id, tenant_id, document_id, page_no, image_uri, width, height) VALUES (:i,:t,:d,1,'x',1000,1400)"), {"i": f"pg_{doc}", "t": TENANT, "d": doc})
        c.execute(text("INSERT INTO field_schemas (id, tenant_id, doc_type, version, fields) VALUES (:i,:t,'invoice',:v, CAST(:f AS jsonb))"), {"i": sch, "t": TENANT, "v": abs(hash(sch)) % 1000, "f": json.dumps([{"name": "total_amount", "label": "合計金額(税込)", "type": "money_jpy", "critical": True}])})
        c.execute(text("INSERT INTO extraction_runs (id, tenant_id, document_id, schema_id, status, engine_versions) VALUES (:i,:t,:d,:s,'processing', CAST('{}' AS jsonb))"), {"i": run, "t": TENANT, "d": doc, "s": sch})


def _fields(engine, run: str):  # type: ignore[no-untyped-def]
    with engine.begin() as c:
        rows = c.execute(text("SELECT field_name, value_normalized, review_status FROM extraction_fields WHERE run_id=:r"), {"r": run}).all()
        st = c.execute(text("SELECT status FROM extraction_runs WHERE id=:r"), {"r": run}).scalar()
    return [tuple(r) for r in rows], st


def _worker(conf: float, store: PgContextStore, exports: RedisQueue) -> ExtractionWorker:
    graph = build_graph(
        checkpointer=MemorySaver(),
        adapter=LLMAdapter(FakeProvider(handler=_llm)),
        bundle=PromptBundle.load(default_bundle_dir()),
        memory=MemoryService(HashingEmbedder(), InMemoryMemoryRepository()),
        structure_client=_FakeStructure(conf),
        image_loader=lambda uri: b"png",
        context_store=store,
        export_enqueue=exports.enqueue,
    )
    consumer = RedisStreamConsumer(REDIS, "q.extract", "orchestrator", "worker-1")
    return ExtractionWorker(graph, store, consumer, webhook=lambda ev, d: print(f"  webhook: {ev}"))


def main() -> int:
    engine = create_engine(DSN, future=True)
    store = PgContextStore(DSN)
    exports = RedisQueue(REDIS)
    gw_queue = GwRedisQueue(REDIS)  # gateway 本番アダプタ（enqueue）
    orch = QueueOrchestratorClient(gw_queue)  # gateway → resume ジョブ発行（§4.4）
    ok = True

    # --- Phase A: 自動確定 ---
    print("== Phase A: 自動確定 ==")
    _seed(engine, "doc_a", "run_a", "sch_a")
    gw_queue.enqueue("q.extract", {"run_id": "run_a", "tenant_id": TENANT})
    worker_a = _worker(0.99, store, exports)
    consumer_a = RedisStreamConsumer(REDIS, "q.extract", "orchestrator", "worker-1")
    for mid, payload in consumer_a.consume(count=10):
        if payload.get("run_id") != "run_a":
            continue
        print(f"  process {payload} -> {worker_a.process(payload)}")
        consumer_a.ack(mid)
    rows, st = _fields(engine, "run_a")
    print(f"  extraction_fields={rows} run.status={st}")
    ok &= st == "confirmed" and any(r[0] == "total_amount" and r[1] == "128000" for r in rows)

    # --- Phase B: HITL（needs_review → resume）---
    print("== Phase B: HITL needs_review -> resume ==")
    _seed(engine, "doc_b", "run_b", "sch_b")
    worker_b = _worker(0.78, store, exports)  # 低信頼で interrupt
    s1 = worker_b.process({"run_id": "run_b", "tenant_id": TENANT})
    rows1, st1 = _fields(engine, "run_b")
    print(f"  extract -> {s1}; fields={rows1} run.status={st1}")
    ok &= s1 == "needs_review" and st1 == "needs_review"

    # レビュアが 178000 に修正して確定 → gateway が resume ジョブを Redis に発行
    orch.resume("run_b", TENANT, {"corrections": [{"field_name": "total_amount", "corrected_value": "178000"}]})
    consumer_b = RedisStreamConsumer(REDIS, "q.extract", "orchestrator", "worker-1")
    for mid, payload in consumer_b.consume(count=10):
        if payload.get("run_id") != "run_b" or "resume" not in payload:
            continue
        print(f"  process resume {payload.get('run_id')} -> {worker_b.process(payload)}")
        consumer_b.ack(mid)
    rows2, st2 = _fields(engine, "run_b")
    print(f"  after resume: fields={rows2} run.status={st2}")
    saved = {r[0]: r[1] for r in rows2}
    ok &= st2 == "confirmed" and saved.get("total_amount") == "178000"

    print("=" * 50)
    print("E2E RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
