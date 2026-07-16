"""Prometheus メトリクス（§12.1）。

名前・ラベル・型は設計書 §12.1 の表がそのまま契約。gateway と orchestrator-worker の
両方から使うため、定義をここ 1 箇所に置く（別々に定義すると名前が割れて、
同じ指標のつもりが別系列になる）。

gauge 系（stp_rate / field_pending_ratio / fallback_page_ratio）は「日次の集計値」で、
リクエスト毎に出せる値ではない。集計元は DB なので、収集は §12.1 の意図どおり
定期ジョブ（scripts/collect_metrics.py）が計算して set する。
"""

from newfan_metrics.context import current_tenant, tenant_scope
from newfan_metrics.registry import (
    REGISTRY,
    correction_reuse_hits_total,
    fallback_page_ratio,
    field_pending_ratio,
    llm_cost_jpy_total,
    llm_tokens_total,
    ocr_page_latency_seconds,
    ocr_pages_total,
    render_latest,
    review_time_seconds,
    rule_auto_apply_total,
    run_duration_seconds,
    stp_rate,
    webhook_delivery_failures_total,
)

__all__ = [
    "REGISTRY",
    "current_tenant",
    "tenant_scope",
    "render_latest",
    "ocr_pages_total",
    "ocr_page_latency_seconds",
    "run_duration_seconds",
    "stp_rate",
    "field_pending_ratio",
    "fallback_page_ratio",
    "llm_tokens_total",
    "llm_cost_jpy_total",
    "correction_reuse_hits_total",
    "rule_auto_apply_total",
    "review_time_seconds",
    "webhook_delivery_failures_total",
]
