"""§12.1 の表をそのまま写したメトリクス定義。

名前とラベルは設計書が契約。勝手に変えるとダッシュボードとアラート（§12.3）が壊れる。
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

# 既定のグローバルレジストリを使わない。プロセス内で複数回 import されても
# 「Duplicated timeseries」で落ちないよう自前のレジストリに載せ、テストでも
# 独立した収集ができるようにする。
REGISTRY = CollectorRegistry()

# 秒。OCR は数秒〜数十秒（実測: structure 16.4s）なので既定の bucket（〜10s）では
# 上端に張り付いて分位が読めない。
_LATENCY_BUCKETS = (0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300)
# Run 全体はさらに長い（HITL 待ちを含まない実行時間）
_RUN_BUCKETS = (1, 5, 10, 30, 60, 120, 300, 600, 1800)
# needs_review → confirmed。人が触る時間なので分〜時間の単位。
_REVIEW_BUCKETS = (30, 60, 300, 900, 1800, 3600, 14400, 86400)

ocr_pages_total = Counter(
    "ocr_pages_total", "処理ページ数", ["tenant", "engine"], registry=REGISTRY
)
ocr_page_latency_seconds = Histogram(
    "ocr_page_latency_seconds",
    "ページあたりの OCR 所要（structure/ocr/vl 別）",
    ["engine"],
    buckets=_LATENCY_BUCKETS,
    registry=REGISTRY,
)
run_duration_seconds = Histogram(
    "run_duration_seconds",
    "Run 所要",
    ["outcome"],
    buckets=_RUN_BUCKETS,
    registry=REGISTRY,
)
stp_rate = Gauge(
    "stp_rate", "自動確定ドキュメント率（日次）", ["tenant", "doc_type"], registry=REGISTRY
)
field_pending_ratio = Gauge(
    "field_pending_ratio", "pending フィールド率", ["tenant"], registry=REGISTRY
)
fallback_page_ratio = Gauge(
    "fallback_page_ratio", "VL 行きページ率", ["tenant"], registry=REGISTRY
)
llm_tokens_total = Counter(
    "llm_tokens_total", "LLM トークン数（kie/correct/chat 別）",
    ["purpose", "direction"], registry=REGISTRY,
)
llm_cost_jpy_total = Counter(
    "llm_cost_jpy_total", "LLM コスト（円）", ["tenant"], registry=REGISTRY
)
correction_reuse_hits_total = Counter(
    "correction_reuse_hits_total",
    "修正メモリ由来の自動補正が採用された数（§12.1 が価値 KPI と明記）",
    ["tenant"],
    registry=REGISTRY,
)
rule_auto_apply_total = Counter(
    "rule_auto_apply_total", "ルール適用数", ["tenant", "rule_type"], registry=REGISTRY
)
review_time_seconds = Histogram(
    "review_time_seconds",
    "needs_review → confirmed の所要",
    buckets=_REVIEW_BUCKETS,
    registry=REGISTRY,
)
webhook_delivery_failures_total = Counter(
    "webhook_delivery_failures_total", "webhook 配信失敗数", ["tenant"], registry=REGISTRY
)
workflow_runs_total = Counter(
    "workflow_runs_total", "ワークフロー run 数（終端遷移で計上, §16.8）",
    ["workflow", "status"], registry=REGISTRY,
)
workflow_node_duration_seconds = Histogram(
    "workflow_node_duration_seconds",
    "ワークフローノード所要（§16.8）",
    ["type"],
    buckets=_LATENCY_BUCKETS,
    registry=REGISTRY,
)
sink_write_rows_total = Counter(
    "sink_write_rows_total", "sink.db_write の書込み行数（§16.8 / P6）",
    ["connection"], registry=REGISTRY,
)
watch_lag_seconds = Gauge(
    "watch_lag_seconds",
    "トリガー SQS の最古メッセージ滞留秒（ApproximateAgeOfOldestMessage, §16.8 / P8）",
    ["connection"], registry=REGISTRY,
)


def render_latest() -> bytes:
    """/metrics のボディ（text/plain; version=0.0.4）。"""
    return generate_latest(REGISTRY)
