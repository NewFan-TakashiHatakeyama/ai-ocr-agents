"""§12.1 の表が契約。公開される名前・型・ラベルを固定する。

名前を変えるとダッシュボードとアラート（§12.3）が黙って壊れる。壊れても
「グラフが空になる」だけで例外は出ないため、テストで押さえないと気づけない。

検証は render_latest() の出力（＝Prometheus が実際にスクレイプするもの）に対して行う。
prometheus_client は Counter の名前から `_total` を剥がして内部に持ち、公開時に付け直す。
そのため describe().name を見ると `ocr_pages` になり、設計書の `ocr_pages_total` と
一致しない。内部表現ではなく公開表現を契約として扱う。
"""

from __future__ import annotations

import re

import pytest

from newfan_metrics import (
    current_tenant,
    llm_tokens_total,
    ocr_pages_total,
    render_latest,
    tenant_scope,
)

# 設計書 §12.1 の表そのもの（公開名 → 型）
EXPECTED_TYPES = {
    "ocr_pages_total": "counter",
    "ocr_page_latency_seconds": "histogram",
    "run_duration_seconds": "histogram",
    "stp_rate": "gauge",
    "field_pending_ratio": "gauge",
    "fallback_page_ratio": "gauge",
    "llm_tokens_total": "counter",
    "llm_cost_jpy_total": "counter",
    "correction_reuse_hits_total": "counter",
    "rule_auto_apply_total": "counter",
    "review_time_seconds": "histogram",
    "webhook_delivery_failures_total": "counter",
}

# prometheus_client が自動で足す付随メトリクス。設計書の表には無いが実装都合で出る。
_AUTO_SUFFIX = re.compile(r"_(created|bucket|sum|count)$")


def _exposed_types() -> dict[str, str]:
    body = render_latest().decode("utf-8")
    out = {}
    for line in body.splitlines():
        if line.startswith("# TYPE "):
            _, _, name, kind = line.split(" ", 3)
            if not _AUTO_SUFFIX.search(name):
                out[name] = kind
    return out


@pytest.mark.parametrize(("name", "kind"), sorted(EXPECTED_TYPES.items()))
def test_設計書の指標が公開され型が一致する(name: str, kind: str) -> None:
    exposed = _exposed_types()
    assert name in exposed, f"{name} が公開されていない（§12.1 の表と乖離）"
    assert exposed[name] == kind, f"{name} の型が違う"


def test_設計書に無い指標を勝手に増やしていない() -> None:
    # 名前が割れると同じ指標のつもりが別系列になる。増やすなら設計書と一緒に。
    assert set(_exposed_types()) == set(EXPECTED_TYPES)


def test_ラベルは設計書どおり() -> None:
    ocr_pages_total.labels(tenant="ten_1", engine="structure").inc()
    body = render_latest().decode("utf-8")
    assert 'ocr_pages_total{engine="structure",tenant="ten_1"}' in body


def test_llm_tokens_は入出力を分けて数える() -> None:
    # 入出力を分けないと、単価の違う input/output を混ぜた無意味な合計になる
    llm_tokens_total.labels(purpose="kie", direction="input").inc(100)
    llm_tokens_total.labels(purpose="kie", direction="output").inc(20)
    body = render_latest().decode("utf-8")
    assert 'llm_tokens_total{direction="input",purpose="kie"} 100.0' in body
    assert 'llm_tokens_total{direction="output",purpose="kie"} 20.0' in body


def test_tenant_scope_で文脈テナントが切り替わる() -> None:
    assert current_tenant() == "unknown"
    with tenant_scope("ten_a"):
        assert current_tenant() == "ten_a"
        with tenant_scope("ten_b"):
            assert current_tenant() == "ten_b"
        assert current_tenant() == "ten_a"
    assert current_tenant() == "unknown"


def test_tenant_scope_は空文字を_unknown_にする() -> None:
    # ラベルが "" だと Prometheus 上で見分けが付かず、集計から静かに漏れる
    with tenant_scope(""):
        assert current_tenant() == "unknown"


def test_tenant_scope_は例外でも復元する() -> None:
    with pytest.raises(RuntimeError), tenant_scope("ten_x"):
        raise RuntimeError("boom")
    assert current_tenant() == "unknown"
