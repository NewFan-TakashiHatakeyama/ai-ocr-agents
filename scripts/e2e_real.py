"""実 PostgreSQL + Redis での E2E（§9 / §4.4）。

外部境界（GPU/LLM）のみ Fake、DB/キュー/チェックポイントは本番相当。
  Phase A（自動確定）: enqueue → worker → extraction_fields 保存 + status=confirmed。
  Phase B（HITL）    : 低信頼 → needs_review interrupt → 修正 resume ジョブ → confirmed。
  Phase C（export）  : export worker が q.export を消費 → run_a の canonical JSON を書く。
                       （A/B の finalize が積んだ q.export を読むので、A が落ちると C も落ちる）
  Phase D（除外領域） : exclude_regions 付きスキーマ → span/セルの決定論除外 → metrics.region
                       → 集約 ReviewItem で needs_review。テキスト項目の bbox（F-0）も保存される。
                       （設計 region-template-editor §8 E2E。検証画面の表示は手動 QA）

終了コード（CI の e2e ジョブはこれだけで合否を決める。.github/workflows/ci.yml）:
  全 Phase が PASS のときだけ 0、それ以外は 1。Phase の途中で例外が出たらトレースバックを
  出してその Phase を FAIL にし、残りの Phase も続けて実行する（最初の例外で後続の成否が
  見えなくなるのを避けるため。握りつぶして PASS にすることはない）。

前提:
  - DB は alembic upgrade head 済み（CI は空の Pg に migration を当ててから走らせる）。
  - tenant ten_e2e の doc_a/b/d・run_a/b/d・sch_a/b/d を毎回消して作り直す（所有者接続）。
  - compose の orchestrator-worker が動いていると q.extract のジョブを横取りされて
    A/B が落ちるので、止めてから実行する。

実行（CI の e2e ジョブと同じ手順。詳細は tests/README.md「実 PG + Redis の E2E」）:
    docker ps -q --filter name=orchestrator-worker   # 何も出なければ停止中
    uv sync --frozen --all-packages --all-extras
    DATABASE_URL=postgresql+psycopg://newfan:newfan@localhost:5433/newfan \
    REDIS_URL=redis://localhost:6380 \
    uv run --no-sync python scripts/e2e_real.py
  （`uv run` だけでは --with で redis・psycopg を足しても workspace のメンバー newfan_* が
    入らず import で落ちる。uv sync 済みの venv の python に各 src を PYTHONPATH で通す方法と、
    使い捨て DB で回す方法も tests/README.md）
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from typing import Any, Callable

from langgraph.checkpoint.memory import MemorySaver
from sqlalchemy import create_engine, text

from newfan_gateway.prod import QueueOrchestratorClient, RedisQueue as GwRedisQueue
from newfan_llm_adapter import FakeProvider, LLMAdapter, PromptBundle, default_bundle_dir
from newfan_memory import HashingEmbedder, InMemoryMemoryRepository, MemoryService
from newfan_orchestrator.graph import build_graph
from newfan_orchestrator.pg_persistence import PgContextStore
from newfan_orchestrator.redis_io import RedisQueue, RedisStreamConsumer
from newfan_orchestrator.serde import newfan_serde
from newfan_orchestrator.worker import ExtractionWorker
from newfan_paddle_client import LayoutParsingResponse

DSN = os.environ["DATABASE_URL"]
REDIS = os.environ["REDIS_URL"]
TENANT = "ten_e2e"
PHASES = ("A", "B", "C", "D")

# field_schemas は (tenant_id, doc_type, version) が UNIQUE。以前は abs(hash(sch)) % 1000 で
# 版を決めていたが、str の hash はプロセスごとに乱数化される（PYTHONHASHSEED）ため、
# 3 つの版が衝突したり、前回の実行で残った別 id の版と衝突したりして、まれに
# IntegrityError で落ちていた（CI では約 0.3%、残骸のある手元では約 0.6%）。固定値にする。
_SCHEMA_VERSION = {"sch_a": 1, "sch_b": 2, "sch_d": 3}


def _layout(conf: float) -> dict[str, Any]:
    return {
        "layoutParsingResults": [
            {
                "prunedResult": {
                    "parsing_res_list": [
                        {"block_bbox": [40, 40, 520, 90], "block_label": "text", "block_content": "x", "block_id": 0, "block_order": 0}
                    ],
                    "overall_ocr_res": {
                        "rec_texts": ["128000", "品名", "数量", "りんご", "3"],
                        "rec_scores": [conf, 0.99, 0.98, 0.95, 0.9],
                        "rec_polys": [
                            [[300, 180], [430, 180], [430, 212], [300, 212]],
                            [[20, 300], [120, 300], [120, 320], [20, 320]],
                            [[140, 300], [240, 300], [240, 320], [140, 320]],
                            [[20, 340], [120, 340], [120, 360], [20, 360]],
                            [[140, 340], [240, 340], [240, 360], [140, 360]],
                        ],
                    },
                    "table_res_list": [
                        {
                            "pred_html": "<html><body><table><tbody><tr><td>品名</td><td>数量</td></tr><tr><td>りんご</td><td>3</td></tr></tbody></table></body></html>",
                            "cell_box_list": [[10, 295, 130, 325], [130, 295, 250, 325], [10, 335, 130, 365], [130, 335, 250, 365]],
                            "table_ocr_pred": {"rec_texts": ["品名", "数量", "りんご", "3"], "rec_scores": [0.99, 0.98, 0.95, 0.9]},
                        }
                    ],
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


def _seed(engine, doc: str, run: str, sch: str, exclude_regions: list[dict[str, Any]] | None = None) -> None:  # type: ignore[no-untyped-def]
    """tenant / document（1 ページ 1000×1400）/ schema / run を投入する。

    exclude_regions を渡すと field_schemas.exclude_regions（migration 0007）に載せる
    （Phase D。ページ寸法があるので除外は fail-open にならず実際に効く）。
    """
    with engine.begin() as c:
        c.execute(text("DELETE FROM documents WHERE id = :d"), {"d": doc})
        c.execute(text("DELETE FROM field_schemas WHERE id = :s"), {"s": sch})
        c.execute(text("INSERT INTO tenants (id, name) VALUES (:i,'demo') ON CONFLICT (id) DO NOTHING"), {"i": TENANT})
        c.execute(text("INSERT INTO documents (id, tenant_id, storage_uri, mime_type, page_count, doc_type, status) VALUES (:i,:t,'s3://x','image/png',1,'invoice','processing')"), {"i": doc, "t": TENANT})
        c.execute(text("INSERT INTO pages (id, tenant_id, document_id, page_no, image_uri, width, height) VALUES (:i,:t,:d,1,'x',1000,1400)"), {"i": f"pg_{doc}", "t": TENANT, "d": doc})
        c.execute(
            text(
                "INSERT INTO field_schemas (id, tenant_id, doc_type, version, fields, exclude_regions, source_page_count)"
                " VALUES (:i,:t,'invoice',:v, CAST(:f AS jsonb), CAST(:x AS jsonb), 1)"
            ),
            {
                "i": sch, "t": TENANT, "v": _SCHEMA_VERSION[sch],
                "f": json.dumps([{"name": "total_amount", "label": "合計金額(税込)", "type": "money_jpy", "critical": True}]),
                "x": json.dumps(exclude_regions or [], ensure_ascii=False),
            },
        )
        c.execute(text("INSERT INTO extraction_runs (id, tenant_id, document_id, schema_id, status, engine_versions) VALUES (:i,:t,:d,:s,'processing', CAST('{}' AS jsonb))"), {"i": run, "t": TENANT, "d": doc, "s": sch})


def _fields(engine, run: str):  # type: ignore[no-untyped-def]
    with engine.begin() as c:
        rows = c.execute(text("SELECT field_name, value_normalized, review_status FROM extraction_fields WHERE run_id=:r"), {"r": run}).all()
        st = c.execute(text("SELECT status FROM extraction_runs WHERE id=:r"), {"r": run}).scalar()
    return [tuple(r) for r in rows], st


def _worker(conf: float, store: PgContextStore, exports: RedisQueue) -> ExtractionWorker:
    graph = build_graph(
        checkpointer=MemorySaver(serde=newfan_serde()),
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


class _Env:
    """各 Phase が共有する DB / キューの接続（本番アダプタそのもの）。"""

    def __init__(self) -> None:
        self.engine = create_engine(DSN, future=True)
        self.store = PgContextStore(DSN)
        self.exports = RedisQueue(REDIS)
        self.gw_queue = GwRedisQueue(REDIS)  # gateway 本番アダプタ（enqueue）
        self.orch = QueueOrchestratorClient(self.gw_queue)  # gateway → resume ジョブ発行（§4.4）


def _phase_a(env: _Env) -> bool:
    """自動確定: gateway の enqueue → worker → Pg 保存 → gateway.get_run の読み戻し。"""
    from newfan_gateway.db import PgRepository

    engine = env.engine
    _seed(engine, "doc_a", "run_a", "sch_a")
    env.gw_queue.enqueue("q.extract", {"run_id": "run_a", "tenant_id": TENANT})
    worker_a = _worker(0.99, env.store, env.exports)
    consumer_a = RedisStreamConsumer(REDIS, "q.extract", "orchestrator", "worker-1")
    consumed = False
    for mid, payload in consumer_a.consume(count=10):
        if payload.get("run_id") != "run_a":
            continue
        print(f"  process {payload} -> {worker_a.process(payload)}")
        consumer_a.ack(mid)
        consumed = True
    if not consumed:
        # 別の consumer（compose の orchestrator-worker）に横取りされたか、未処理の
        # ジョブが溜まっていて先頭 10 件に届かなかった
        print("  [FAIL] q.extract から run_a のジョブを取れなかった")
    ok = consumed
    rows, st = _fields(engine, "run_a")
    print(f"  extraction_fields={rows} run.status={st}")
    ok &= st == "confirmed" and any(r[0] == "total_amount" and r[1] == "128000" for r in rows)
    # 構造由来テーブルが extraction_tables に永続化されたか（§5.3）
    with engine.begin() as c:
        trows = c.execute(text("SELECT name, page_no, rows FROM extraction_tables WHERE run_id='run_a'"), {}).all()
    print(f"  extraction_tables={[(r[0], r[1], len(r[2])) for r in trows]}")
    ok &= len(trows) == 1 and trows[0][0] == "table" and len(trows[0][2]) == 1
    tbl_ok = bool(trows) and any(cell.get("value") == "りんご" for row in trows[0][2] for cell in row.values())
    ok &= tbl_ok
    # gateway result 同期: PgRepository.get_run が正規化テーブル（worker 書込）を反映するか
    gw_run = PgRepository(DSN).get_run(TENANT, "run_a")
    if gw_run is None:
        print("  [FAIL] gateway.get_run(run_a) が None")
        return False
    gw_fields = [(f.name, f.label, f.value_normalized) for f in gw_run.fields]
    print(f"  gateway.get_run fields={gw_fields}")
    print(f"  gateway.get_run tables={[(t.name, len(t.rows)) for t in gw_run.tables]} review_summary={gw_run.review_summary}")
    ok &= any(f.name == "total_amount" and f.label == "合計金額(税込)" and f.value_normalized == "128000" for f in gw_run.fields)
    ok &= len(gw_run.tables) == 1 and bool(gw_run.tables[0].rows)
    return ok


def _phase_b(env: _Env) -> bool:
    """HITL: 低信頼で needs_review 停止 → gateway の resume ジョブ（Redis）→ confirmed。"""
    _seed(env.engine, "doc_b", "run_b", "sch_b")
    worker_b = _worker(0.78, env.store, env.exports)  # 低信頼で interrupt
    s1 = worker_b.process({"run_id": "run_b", "tenant_id": TENANT})
    rows1, st1 = _fields(env.engine, "run_b")
    print(f"  extract -> {s1}; fields={rows1} run.status={st1}")
    # 停止していなければ resume は何も検証しない（ここも Phase B の成否に含める）
    ok = s1 == "needs_review" and st1 == "needs_review"

    # レビュアが 178000 に修正して確定 → gateway が resume ジョブを Redis に発行
    env.orch.resume("run_b", TENANT, {"corrections": [{"field_name": "total_amount", "corrected_value": "178000"}]})
    consumer_b = RedisStreamConsumer(REDIS, "q.extract", "orchestrator", "worker-1")
    consumed = False
    for mid, payload in consumer_b.consume(count=10):
        if payload.get("run_id") != "run_b" or "resume" not in payload:
            continue
        print(f"  process resume {payload.get('run_id')} -> {worker_b.process(payload)}")
        consumer_b.ack(mid)
        consumed = True
    if not consumed:
        print("  [FAIL] q.extract から run_b の resume ジョブを取れなかった")
    ok &= consumed
    rows2, st2 = _fields(env.engine, "run_b")
    print(f"  after resume: fields={rows2} run.status={st2}")
    saved = {r[0]: r[1] for r in rows2}
    ok &= st2 == "confirmed" and saved.get("total_amount") == "178000"
    return ok


def _phase_c(env: _Env) -> bool:
    """export worker が q.export を消費し canonical JSON を書く。"""
    import tempfile
    from pathlib import Path

    from newfan_export.pg_source import PgExportSource
    from newfan_export.redis_io import RedisStreamConsumer as ExportConsumer
    from newfan_export.service import ExportService
    from newfan_export.storage import LocalObjectStore
    from newfan_export.webhook import WebhookSender
    from newfan_export.worker import ExportWorker

    outdir = Path(tempfile.mkdtemp())
    ex_worker = ExportWorker(
        PgExportSource(DSN),
        ExportService(LocalObjectStore(outdir), WebhookSender()),
        ExportConsumer(REDIS, "q.export", "export", "export-1"),
    )
    # 共有の Redis（compose）には他の run の export が溜まっていることがあり、1 回の
    # run_once では run_a の分に届かない。キューが空になるか run_a.json が出るまで回す
    n_exported = 0
    run_a_json = None
    for _ in range(500):  # 計測で消した run の export が数千件残っていることがある
        n = ex_worker.run_once()
        n_exported += n
        run_a_json = next((p for p in outdir.rglob("*.json") if p.name == "run_a.json"), None)
        if run_a_json is not None or n == 0:
            break
    canon = list(outdir.rglob("*.json"))
    print(f"  processed={n_exported} canonical={[str(p.relative_to(outdir)) for p in canon][-5:]}")
    if run_a_json is not None:
        doc = json.loads(run_a_json.read_text(encoding="utf-8"))
        print(f"  run_a canonical keys={list(doc)}")
    return n_exported >= 1 and run_a_json is not None


def _phase_d(env: _Env) -> bool:
    """除外領域（decision D1/D8）+ F-0 のテキスト項目 bbox。

    ページ 1000×1400 のうち左上の [0, 280]〜[130, 378] px を除外する。_layout の span では
    「品名」[20,300,120,320] と「りんご」[20,340,120,360] が過半を覆われて落ち、
    表の「りんご」セル [10,335,130,365] は値が空になる（列は残る。数量「3」は残る）。
    「128000」[300,180,430,212] は領域外なので total_amount はそのまま取れる。
    """
    from newfan_gateway.db import PgAdminRepository, PgRepository
    from newfan_schemas import resolve_regions

    engine = env.engine
    stamp = {"page": 1, "rect": [0.0, 0.20, 0.13, 0.27], "label": "stamp"}
    _seed(engine, "doc_d", "run_d", "sch_d", exclude_regions=[stamp])
    worker_d = _worker(0.99, env.store, env.exports)  # 高信頼: 除外のセルマスクだけで needs_review になる
    s_d = worker_d.process({"run_id": "run_d", "tenant_id": TENANT})
    rows_d, st_d = _fields(engine, "run_d")
    with engine.begin() as c:
        region = c.execute(text("SELECT metrics->'region' FROM extraction_runs WHERE id='run_d'")).scalar()
        bbox_row = c.execute(
            text("SELECT page_no, bbox FROM extraction_fields WHERE run_id='run_d' AND field_name='total_amount'")
        ).first()
        trows_d = c.execute(text("SELECT rows FROM extraction_tables WHERE run_id='run_d'")).all()
    print(f"  extract -> {s_d}; fields={rows_d} run.status={st_d}")
    print(f"  metrics.region={region}")
    print(f"  total_amount page/bbox={tuple(bbox_row) if bbox_row else None}")
    d_ok = True
    # セルマスクの集約 ReviewItem（§5.4）で needs_review に倒れる。値は消えない
    d_ok &= s_d == "needs_review" and st_d == "needs_review"
    d_ok &= any(r[0] == "total_amount" and r[1] == "128000" for r in rows_d)
    # 除外の観測値（needs_review 保存時点で載っていること = §5.4 の 5 点目）
    d_ok &= region is not None and region.get("excluded_spans") == 2 and region.get("excluded_cells") == 1
    d_ok &= region is not None and region.get("excluded_rows") == 0 and region.get("skipped_pages_no_dims") == []
    # F-0: テキスト項目の bbox が根拠 span から合成されて保存される
    d_ok &= bbox_row is not None and bbox_row[0] == 1 and list(bbox_row[1]) == [300, 180, 430, 212]
    # セルは削除ではなく空化（列ズレ防止）。行は残る
    if trows_d:
        cells = [cell for row in trows_d[0][0] for cell in row.values()]
        print(f"  table rows={len(trows_d[0][0])} masked_cells={[c for c in cells if not c.get('value')]}")
        d_ok &= len(trows_d[0][0]) == 1 and sum(1 for c in cells if not c.get("value")) == 1
    else:
        d_ok = False
    # gateway 側の到達経路: region_stats（run metrics 由来）と、ページ解決済みの除外領域（スキーマ由来）
    gw_run_d = PgRepository(DSN).get_run(TENANT, "run_d")
    d_ok &= gw_run_d is not None and gw_run_d.region_stats == region
    sch_d = PgAdminRepository(DSN).get_schema_by_id(TENANT, "sch_d")
    applied = resolve_regions(list(sch_d.exclude_regions), 1) if sch_d else []
    print(f"  applied_exclude_regions={applied}")
    d_ok &= applied == [{"page_no": 1, "rect": stamp["rect"], "label": "stamp"}]
    return d_ok


def _run_phase(name: str, title: str, fn: Callable[[], bool]) -> bool:
    """1 Phase を実行して成否を返す。

    例外はトレースバックを出して FAIL にする（後続 Phase の成否も出すため、ここで止めない）。
    戻り値は ``True`` そのものだけを PASS とみなす（None や真偽以外が返る書き間違いを
    PASS に倒さない）。
    """
    print(f"== Phase {name}: {title} ==", flush=True)
    try:
        result = fn()
    except Exception:  # noqa: BLE001 - FAIL として記録し、終了コードに反映する
        # stdout（パイプ時はブロックバッファ）と stderr の順序を CI のログで崩さない
        sys.stdout.flush()
        traceback.print_exc()
        sys.stderr.flush()
        print(f"  [FAIL] Phase {name}: 例外で中断", flush=True)
        return False
    return result is True


def main() -> int:
    env = _Env()
    phase_ok: dict[str, bool] = {}
    phase_ok["A"] = _run_phase("A", "自動確定", lambda: _phase_a(env))
    phase_ok["B"] = _run_phase("B", "HITL needs_review -> resume", lambda: _phase_b(env))
    phase_ok["C"] = _run_phase("C", "export worker q.export -> canonical JSON", lambda: _phase_c(env))
    phase_ok["D"] = _run_phase(
        "D", "exclude regions -> metrics.region / needs_review / field bbox (F-0)", lambda: _phase_d(env)
    )

    # 全 Phase が実行されて全部 PASS のときだけ成功（Phase を足し忘れても緑にしない）
    ok = tuple(phase_ok) == PHASES and all(phase_ok.values())
    print("=" * 50)
    print("phases:", {k: ("PASS" if v else "FAIL") for k, v in phase_ok.items()})
    print("E2E RESULT:", "PASS" if ok else "FAIL", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
