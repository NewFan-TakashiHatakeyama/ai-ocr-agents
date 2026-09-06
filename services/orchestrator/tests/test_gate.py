from newfan_schemas import ExtractedField, FieldSchema

from newfan_orchestrator.gate import Thresholds, confidence_gate, threshold_for


def _schema() -> FieldSchema:
    return FieldSchema.model_validate(
        {
            "doc_type": "invoice",
            "fields": [
                {"name": "total_amount", "type": "money_jpy", "critical": True},
                {"name": "note", "type": "string"},
            ],
        }
    )


def test_threshold_for() -> None:
    t = Thresholds()
    assert threshold_for(True, t) == 0.90
    assert threshold_for(False, t) == 0.80


def test_critical_below_threshold_needs_review() -> None:
    fields = [
        ExtractedField(name="total_amount", confidence=0.85, grounding_score=1.0),
        ExtractedField(name="note", confidence=0.85, grounding_score=1.0),
    ]
    items = confidence_gate(fields, _schema())
    # total_amount(critical, 0.85<0.90) は要レビュー、note(0.85>=0.80) は自動
    names = {i.field_name for i in items}
    assert names == {"total_amount"}
    assert items[0].critical is True


def test_no_grounding_forces_review() -> None:
    fields = [ExtractedField(name="note", confidence=0.99, grounding_score=0.0)]
    items = confidence_gate(fields, _schema())
    assert len(items) == 1
    assert "根拠 span なし" in items[0].reason


def test_always_review_field() -> None:
    fields = [ExtractedField(name="note", confidence=0.99, grounding_score=1.0)]
    items = confidence_gate(fields, _schema(), always_review_fields={"note"})
    assert len(items) == 1
    assert "always_review" in items[0].reason


def test_all_auto_when_above_threshold() -> None:
    fields = [
        ExtractedField(name="total_amount", confidence=0.95, grounding_score=1.0),
        ExtractedField(name="note", confidence=0.90, grounding_score=1.0),
    ]
    assert confidence_gate(fields, _schema()) == []


# --- confidence_gate_node: 所見が人に届くこと（Phase 0 の既存バグ修正） ---
#
# confidence_gate 自体は純ロジックで正しく所見を返していたが、ノード側で
# 2 つ落ちていた: (1) 上流の ReviewItem を置換で捨てる (2) 所見の付いた
# field を pending にしないため検証画面の「要確認」に出ない。


def test_review_items_mark_fields_pending() -> None:
    """gate の所見が付いた field は pending になる（要確認に出る）。

    検証画面は extraction_fields.review_status しか見ないため、ここで
    PENDING を立てないと「run は needs_review なのに画面は全部確定済み」に
    見える（確信度 0.00 の項目が確定済みとして並ぶ実症状）。
    """
    from newfan_schemas import ReviewStatus

    from newfan_orchestrator import nodes

    low = ExtractedField(name="total_amount", value_raw="1", span_ids=[1],
                         confidence=0.10, grounding_score=1.0)
    ok = ExtractedField(name="issuer_name", value_raw="x", span_ids=[2],
                        confidence=0.99, grounding_score=1.0)
    out = nodes.confidence_gate_node(
        {"schema": {"doc_type": "invoice", "fields": []}, "fields": [low, ok]}
    )
    by = {f.name: f for f in out["fields"]}
    assert by["total_amount"].review_status is ReviewStatus.PENDING
    assert by["issuer_name"].review_status is ReviewStatus.AUTO  # 所見なしは触らない
    assert [i.field_name for i in out["review_items"]] == ["total_amount"]


def test_gate_does_not_downgrade_human_confirmed_fields() -> None:
    """人手確定（corrected/approved）は pending に差し戻さない。

    resume 後の apply_feedback で確定した値を、再走時の gate が
    「まだ確信度が低い」という理由で未確認へ戻すと人の作業が消える。
    """
    from newfan_schemas import ReviewStatus

    from newfan_orchestrator import nodes

    fixed = ExtractedField(name="total_amount", value_raw="1", span_ids=[1],
                           confidence=0.10, grounding_score=1.0,
                           review_status=ReviewStatus.CORRECTED)
    out = nodes.confidence_gate_node(
        {"schema": {"doc_type": "invoice", "fields": []}, "fields": [fixed]}
    )
    assert out["fields"][0].review_status is ReviewStatus.CORRECTED


def test_gate_carries_forward_vl_fallback_review_items() -> None:
    """上流（vl_fallback）の ReviewItem を捨てない。

    review_items は reducer 無しの LastValue チャネルなので、gate が置換
    return すると「VL 失敗で未抽出のページ」の警告がグラフ通過時に消える。
    ノード単体テストは戻り値しか見ないため長く検出できていなかった。
    """
    from newfan_schemas import ReviewItem

    from newfan_orchestrator import nodes

    upstream = ReviewItem(field_name="__page_2", reason="VL結果なし（未抽出ページ）")
    low = ExtractedField(name="total_amount", value_raw="1", span_ids=[1],
                         confidence=0.10, grounding_score=1.0)
    out = nodes.confidence_gate_node(
        {
            "schema": {"doc_type": "invoice", "fields": []},
            "fields": [low],
            "review_items": [upstream],
        }
    )
    names = [i.field_name for i in out["review_items"]]
    assert names == ["__page_2", "total_amount"]  # 上流が先、gate 所見が後
    assert nodes.route_confidence_gate(out) == "hitl_review"


def test_gate_dedups_identical_review_items() -> None:
    """同一 (field_name, reason) は重複させない（再走で二重に見せない）。"""
    from newfan_orchestrator import nodes

    low = ExtractedField(name="total_amount", value_raw="1", span_ids=[1],
                         confidence=0.10, grounding_score=1.0)
    once = nodes.confidence_gate_node(
        {"schema": {"doc_type": "invoice", "fields": []}, "fields": [low]}
    )
    twice = nodes.confidence_gate_node(
        {
            "schema": {"doc_type": "invoice", "fields": []},
            "fields": [low],
            "review_items": once["review_items"],
        }
    )
    assert len(twice["review_items"]) == len(once["review_items"]) == 1


# ---------- 除外領域の観測性と位置ガード（設計 §5.4 / §5.5） ----------

_W = _H = 1000
_PAGES = [{"page_no": 1, "width": _W, "height": _H}]
_REGION_SCHEMA = {
    "doc_type": "invoice",
    "fields": [
        {"name": "title", "type": "string", "region": {"page": 1, "rect": [0.3, 0.0, 0.7, 0.1]}}
    ],
}


def _good_field(name: str = "title") -> ExtractedField:
    """位置も確信度も問題ない field（gate 自身の所見を出さないための土台）。"""
    return ExtractedField(
        name=name, value_raw="請求書", value_normalized="請求書", span_ids=[1],
        page=1, bbox=[320, 10, 680, 60], confidence=0.99, grounding_score=1.0,
    )


def _gate(state: dict):
    from newfan_orchestrator import nodes

    return nodes.confidence_gate_node(state)


def test_mask_stats_emit_aggregated_review_item() -> None:
    """セル/行マスクが起きた run は集約 ReviewItem を積み hitl へ回す。

    明細に領域が重なっているのが運用上いちばん痛い誤設定なので、必ず人の目に触れさせる。
    """
    from newfan_orchestrator import nodes

    out = _gate(
        {
            "schema": {"doc_type": "invoice", "fields": []},
            "fields": [_good_field()],
            "metrics": {"region": {"excluded_cells": 3, "excluded_rows": 1}},
        }
    )
    reasons = [i.reason for i in out["review_items"]]
    assert any("3セル/1行を未取込" in r for r in reasons)
    assert nodes.route_confidence_gate(out) == "hitl_review"


def test_no_review_item_for_ordinary_span_exclusion() -> None:
    """印影ゴミの除外は想定内動作。metrics だけで ReviewItem は積まない。"""
    from newfan_orchestrator import nodes

    out = _gate(
        {
            "schema": {"doc_type": "invoice", "fields": []},
            "fields": [_good_field()],
            "spans": [object()] * 100,
            "metrics": {"region": {"excluded_spans": 3}},
        }
    )
    assert out["review_items"] == []
    assert nodes.route_confidence_gate(out) == "finalize"


def test_excessive_span_exclusion_emits_review_item() -> None:
    """除外 span が全体の 20% を超えたら本文に重なっている疑いを出す。"""
    out = _gate(
        {
            "schema": {"doc_type": "invoice", "fields": []},
            "fields": [_good_field()],
            "spans": [object()] * 10,
            "metrics": {"region": {"excluded_spans": 5}},
        }
    )
    assert any("本文に重なっている可能性" in i.reason for i in out["review_items"])


def test_excluded_span_with_null_required_field_emits_review_item() -> None:
    """除外が起きた run で必須項目が空 = 実データを消した疑い（C19）。

    除外は doc_type 単位なので、同じ doc_type を共有する別レイアウト取引先の
    帳票にも同座標が当たる。その最低限の検知線。
    """
    empty_required = ExtractedField(name="total_amount", value_raw=None, page=1)
    out = _gate(
        {
            "schema": {
                "doc_type": "invoice",
                "fields": [{"name": "total_amount", "type": "money_jpy", "required": True}],
            },
            "fields": [empty_required],
            "spans": [object()] * 100,
            "metrics": {"region": {"excluded_spans": 2}},
        }
    )
    assert any("必須項目を消した可能性" in i.reason for i in out["review_items"])


def test_region_guard_shadow_records_metrics_only(monkeypatch) -> None:
    """既定（shadow）は metrics とログだけ。confidence も review_items も触らない。"""
    monkeypatch.delenv("REGION_GUARD_ENFORCE", raising=False)
    far = _good_field()
    far.bbox = [10, 800, 200, 860]
    before = far.confidence
    out = _gate(
        {"schema": _REGION_SCHEMA, "fields": [far], "pages": _PAGES, "source_page_count": 1}
    )
    assert out["metrics"]["region"]["mismatch_fields"] == ["title"]
    assert out["review_items"] == []
    assert out["fields"][0].confidence == before
    assert out["fields"][0].review_status.value != "pending"
    # **検証画面に参考表示するようになっても、run は needs_review に倒れない。**
    # 「表示しただけのつもりがレビューが増えた」という疑いへの機械的な回答。
    from newfan_orchestrator import nodes

    assert nodes.route_confidence_gate(out) == "finalize"


def test_region_guard_enforced_single_field_reviews(monkeypatch) -> None:
    """有効化すると per-field の所見が出て、検証画面の「要確認」にも載る。"""
    monkeypatch.setenv("REGION_GUARD_ENFORCE", "1")
    far = _good_field()
    far.bbox = [10, 800, 200, 860]
    out = _gate(
        {"schema": _REGION_SCHEMA, "fields": [far], "pages": _PAGES, "source_page_count": 1}
    )
    assert [(i.field_name, i.reason) for i in out["review_items"]] == [
        ("title", "設定領域外の位置で検出")
    ]
    assert out["fields"][0].review_status.value == "pending"
    # 値は決して捨てない（領域は hint であって hard crop ではない）
    assert out["fields"][0].value_normalized == "請求書"


def _multi_region_schema(n: int) -> dict:
    return {
        "doc_type": "invoice",
        "fields": [
            {"name": f"f{i}", "type": "string", "region": {"page": 1, "rect": [0.3, 0.0, 0.7, 0.1]}}
            for i in range(n)
        ],
    }


def _far_fields(n: int) -> list[ExtractedField]:
    out = []
    for i in range(n):
        f = _good_field(f"f{i}")
        f.bbox = [10, 800, 200, 860]
        out.append(f)
    return out


def test_region_guard_enforced_majority_mismatch_suppresses_per_field(monkeypatch) -> None:
    """region 3 件以上で過半がずれたら「別レイアウトの帳票」と見て抑止する。

    取引先 B の帳票を全件レビュー化させないための判定。
    """
    monkeypatch.setenv("REGION_GUARD_ENFORCE", "1")
    out = _gate(
        {
            "schema": _multi_region_schema(3),
            "fields": _far_fields(3),
            "pages": _PAGES,
            "source_page_count": 1,
        }
    )
    assert out["metrics"]["region"]["layout_mismatch"] is True
    assert out["review_items"] == []


def test_region_guard_layout_judgement_requires_min_fields(monkeypatch) -> None:
    """n<=2 では doc レベル抑止を行わず per-field 所見を出す（C33）。

    n=1 なら 1 件の mismatch が常に「過半」になり、抑止が常に効いてガードが
    一度もレビューを出さない（enforce しても shadow と挙動が変わらない）。
    """
    monkeypatch.setenv("REGION_GUARD_ENFORCE", "1")
    for n in (1, 2):
        out = _gate(
            {
                "schema": _multi_region_schema(n),
                "fields": _far_fields(n),
                "pages": _PAGES,
                "source_page_count": 1,
            }
        )
        assert out["metrics"]["region"]["layout_mismatch"] is False, n
        assert len(out["review_items"]) == n, n


def test_region_guard_page_count_drift_ignores_page(monkeypatch) -> None:
    """テンプレート化時と run のページ数が違えば page 一致は問わない。"""
    monkeypatch.setenv("REGION_GUARD_ENFORCE", "1")
    pages2 = [
        {"page_no": 1, "width": _W, "height": _H},
        {"page_no": 2, "width": _W, "height": _H},
    ]
    f = _good_field()
    f.page = 2  # 2 ページ目で、座標は領域どおり
    out = _gate(
        {"schema": _REGION_SCHEMA, "fields": [f], "pages": pages2, "source_page_count": 1}
    )
    assert "mismatch_fields" not in out["metrics"].get("region", {})
    assert out["review_items"] == []


def test_region_guard_no_source_page_count_skips_page_judgement(monkeypatch) -> None:
    monkeypatch.setenv("REGION_GUARD_ENFORCE", "1")
    pages2 = [
        {"page_no": 1, "width": _W, "height": _H},
        {"page_no": 2, "width": _W, "height": _H},
    ]
    f = _good_field()
    f.page = 2
    out = _gate({"schema": _REGION_SCHEMA, "fields": [f], "pages": pages2})
    assert out["review_items"] == []


def test_region_observations_noop_without_regions() -> None:
    """領域機能を使っていない run では metrics に region キーを作らない。"""
    out = _gate({"schema": {"doc_type": "invoice", "fields": []}, "fields": [_good_field()]})
    assert "region" not in out["metrics"]


def test_読み取れなかったページがある_run_は自動確定しない() -> None:
    """ページ処理の失敗は errors に積んで継続する設計だが、errors は永続化も表示も
    されない。そのままだと**ページが欠けた結果が自動確定され会計連携まで素通りする**。
    実際に 3 ページ PDF で 2 ページが落ち、合計金額に別ページの数字が入った。
    """
    from newfan_orchestrator import nodes
    from newfan_schemas import Span

    out = _gate(
        {
            "schema": {"doc_type": "invoice", "fields": []},
            "fields": [_good_field()],
            "spans": [Span(span_id=1, page=2, text="x", conf=0.9, bbox=[0, 0, 5, 5])],
            "errors": [
                {"page": 1, "stage": "structure_ocr", "error": "boom"},
                {"page": 3, "stage": "structure_ocr", "error": "boom"},
                {"stage": "load_context", "code": "E2000"},  # ページ不明は数えない
            ],
        }
    )
    reasons = [i.reason for i in out["review_items"]]
    assert any("p.1・p.3" in r for r in reasons), reasons
    assert nodes.route_confidence_gate(out) == "hitl_review"


def test_VLで拾えたページは欠落として扱わない() -> None:
    """structure_ocr が落ちても VL フォールバックで span が取れていれば結果は欠けない。"""
    from newfan_orchestrator import nodes
    from newfan_schemas import Span

    out = _gate(
        {
            "schema": {"doc_type": "invoice", "fields": []},
            "fields": [_good_field()],
            "spans": [Span(span_id=1, page=1, text="x", conf=0.9, bbox=[0, 0, 5, 5])],
            "errors": [{"page": 1, "stage": "structure_ocr", "error": "boom"}],
        }
    )
    assert out["review_items"] == []
    assert nodes.route_confidence_gate(out) == "finalize"


def test_解消した_mismatch_が前回実行から残らない(monkeypatch) -> None:
    """再配信で mismatch が解消しても前回の mismatch_fields を残すと、
    Phase 5 の許容パラメータを決める shadow 実測が実際より悪く見える。
    """
    monkeypatch.delenv("REGION_GUARD_ENFORCE", raising=False)
    out = _gate(
        {
            "schema": _REGION_SCHEMA,
            "fields": [_good_field()],  # 領域内に収まっている
            "pages": _PAGES,
            "source_page_count": 1,
            "metrics": {
                "region": {
                    "excluded_spans": 1,
                    "mismatch_fields": ["title"],  # 前回実行の残骸
                    "layout_mismatch": True,
                }
            },
        }
    )
    region = out["metrics"]["region"]
    assert "mismatch_fields" not in region, region
    assert "layout_mismatch" not in region, region
    assert region["excluded_spans"] == 1  # 除外件数は消さない
