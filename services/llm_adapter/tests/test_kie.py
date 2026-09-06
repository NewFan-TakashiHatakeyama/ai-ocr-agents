import json

from newfan_schemas import Span

from newfan_llm_adapter import FakeProvider, LLMAdapter, PromptBundle, kie_extract

_SPANS = [
    Span(span_id=10, page=1, text="御請求金額", conf=0.99, bbox=[0, 0, 1, 1]),
    Span(span_id=11, page=1, text="¥128,000", conf=0.72, bbox=[0, 0, 1, 1]),
]

_SCHEMA = {"doc_type": "invoice", "fields": [{"name": "total_amount", "type": "money_jpy"}]}


def _kie_response(fields: list[dict], tables: list[dict] | None = None) -> str:
    return json.dumps(
        {"fields": fields, "tables": tables or [], "unmapped_required": []},
        ensure_ascii=False,
    )


def test_kie_maps_fields_with_valid_spans(bundle: PromptBundle) -> None:
    resp = _kie_response(
        [{"name": "total_amount", "value": "128000", "span_ids": [11], "page": 1}]
    )
    adapter = LLMAdapter(FakeProvider([resp]))
    result = kie_extract(
        adapter, bundle, spans=_SPANS, layout_markdown="# 請求書", schema_json=_SCHEMA
    )
    assert len(result.fields) == 1
    f = result.fields[0]
    assert f.name == "total_amount"
    assert f.value_raw == "128000"
    assert f.span_ids == [11]
    assert f.source_quote == "¥128,000"  # 実在 span テキスト


def test_kie_drops_hallucinated_span_ids(bundle: PromptBundle) -> None:
    # LLM が存在しない span_id=999 を返す → 検証で除去、source_quote なし
    resp = _kie_response(
        [{"name": "total_amount", "value": "999999", "span_ids": [999], "page": 1}]
    )
    adapter = LLMAdapter(FakeProvider([resp]))
    result = kie_extract(
        adapter, bundle, spans=_SPANS, layout_markdown="", schema_json=_SCHEMA
    )
    f = result.fields[0]
    assert f.span_ids == []  # 捏造 span を除去
    assert f.source_quote is None  # 根拠なし → 後段で grounding 0 → レビュー


def test_kie_tables(bundle: PromptBundle) -> None:
    resp = _kie_response(
        [],
        tables=[
            {
                "name": "line_items",
                "rows": [{"cells": {"item": {"value": "A", "span_ids": [10]}}}],
            }
        ],
    )
    adapter = LLMAdapter(FakeProvider([resp]))
    result = kie_extract(
        adapter, bundle, spans=_SPANS, layout_markdown="", schema_json=_SCHEMA
    )
    assert result.tables[0].name == "line_items"
    assert result.tables[0].rows[0]["item"].value == "A"
    assert result.tables[0].rows[0]["item"].span_ids == [10]


def test_kie_discovery_uses_llm_label_when_schema_empty(bundle: PromptBundle) -> None:
    """スキーマレス自動発見（ADR-0006）: LLM 申告の見出し原文を label に使う。

    これが無いと発見項目は snake_case の name だけで表示され、検証画面でも
    テンプレート化ダイアログでも人が項目を同定できない。
    """
    resp = _kie_response(
        [
            {
                "name": "total_amount",
                "label": "御請求金額",
                "value": "128000",
                "span_ids": [11],
                "page": 1,
            }
        ]
    )
    adapter = LLMAdapter(FakeProvider([resp]))
    result = kie_extract(
        adapter,
        bundle,
        spans=_SPANS,
        layout_markdown="",
        schema_json={"doc_type": "", "fields": []},  # スキーマ未指定
    )
    assert result.fields[0].label == "御請求金額"


def test_kie_schema_label_wins_over_llm_label(bundle: PromptBundle) -> None:
    """スキーマ定義がある項目はスキーマの label を正とする（LLM 申告で上書きさせない）。"""
    resp = _kie_response(
        [
            {
                "name": "total_amount",
                "label": "LLMの勝手な見出し",
                "value": "128000",
                "span_ids": [11],
            }
        ]
    )
    adapter = LLMAdapter(FakeProvider([resp]))
    schema = {
        "doc_type": "invoice",
        "fields": [{"name": "total_amount", "label": "合計金額(税込)", "type": "money_jpy"}],
    }
    result = kie_extract(adapter, bundle, spans=_SPANS, layout_markdown="", schema_json=schema)
    assert result.fields[0].label == "合計金額(税込)"


def test_kie_prompt_contains_discovery_mode_instruction(bundle: PromptBundle) -> None:
    """プロンプト本文に自動発見モードの指示が残っていることを pin する。

    バンドル差し替えでこの節が落ちると、スキーマレス抽出が「対象未定義なので
    何も抽出しない」に静かに退行する（fields が空のまま成功に見える）。
    """
    assert "自動発見" in bundle.kie_template
    assert "snake_case" in bundle.kie_template


def test_kie_discovery_dedupes_duplicate_names(bundle: PromptBundle) -> None:
    """自動発見で LLM が同名 name を複数返しても両方保全する（ADR-0006）。

    素通しすると DB の UNIQUE (run_id, field_name) への UPSERT が後勝ちになり、
    先の項目（値・span・label）が警告なく消える。サフィックスで別名化して
    レビュアが両方の値を見て選べるようにする。
    """
    resp = _kie_response(
        [
            {"name": "total_amount", "label": "御請求金額", "value": "128000", "span_ids": [11]},
            {"name": "total_amount", "label": "合計", "value": "99000", "span_ids": [10]},
        ]
    )
    adapter = LLMAdapter(FakeProvider([resp]))
    result = kie_extract(
        adapter, bundle, spans=_SPANS, layout_markdown="",
        schema_json={"doc_type": "", "fields": []},
    )
    names = [f.name for f in result.fields]
    assert names == ["total_amount", "total_amount_2"]
    assert result.fields[0].value_raw == "128000"
    assert result.fields[1].value_raw == "99000"


def test_kie_discovery_sanitizes_label(bundle: PromptBundle) -> None:
    """LLM 申告 label は文字列のみ・制御文字除去・120字打ち切り。

    無検疫だと dict の repr や U+0000 が TEXT 列へ流れ、後者は Pg の DataError で
    save_result ごと落ちてジョブが毒メッセージ化する（敵対的レビュー確定）。
    """
    resp = _kie_response(
        [
            {"name": "a", "label": {"nested": "dict"}, "value": "1", "span_ids": [11]},
            {"name": "b", "label": "御請求\x00金額\x07", "value": "2", "span_ids": [11]},
            {"name": "c", "label": "x" * 500, "value": "3", "span_ids": [11]},
        ]
    )
    adapter = LLMAdapter(FakeProvider([resp]))
    result = kie_extract(
        adapter, bundle, spans=_SPANS, layout_markdown="",
        schema_json={"doc_type": "", "fields": []},
    )
    by = {f.name: f for f in result.fields}
    assert by["a"].label is None  # 非文字列は捨てる
    assert by["b"].label == "御請求金額"  # 制御文字は除去
    assert by["c"].label is not None and len(by["c"].label) == 120  # 打ち切り


def test_kie_schema_mode_never_uses_llm_label(bundle: PromptBundle) -> None:
    """スキーマ指定の抽出では、定義に label が無い項目も LLM 申告で埋めない。

    混ぜると「スキーマ指定なのに表示名が実行ごとに揺れる」（ADR-0006 の
    『行 label はスキーマレス時のみ』の実装保証）。
    """
    resp = _kie_response(
        [{"name": "total_amount", "label": "勝手な見出し", "value": "1", "span_ids": [11]}]
    )
    adapter = LLMAdapter(FakeProvider([resp]))
    schema = {"doc_type": "invoice", "fields": [{"name": "total_amount", "type": "money_jpy"}]}
    result = kie_extract(adapter, bundle, spans=_SPANS, layout_markdown="", schema_json=schema)
    assert result.fields[0].label is None


# --- F-0: 根拠 span からの bbox 合成（設計 §7.2） ---
#
# 長らく kie は span_ids しか付けず bbox を作らなかったため、テキスト項目の
# 座標は保存列があるのに常に NULL で、検証画面のオーバーレイに一切出なかった。

_BOXED = [
    Span(span_id=10, page=1, text="御請求金額", conf=0.99, bbox=[10, 20, 90, 40]),
    Span(span_id=11, page=1, text="¥128,000", conf=0.72, bbox=[100, 25, 180, 45]),
]


def test_field_bbox_single_span(bundle: PromptBundle) -> None:
    resp = _kie_response([{"name": "total_amount", "value": "128000", "span_ids": [11]}])
    adapter = LLMAdapter(FakeProvider([resp]))
    f = kie_extract(
        adapter, bundle, spans=_BOXED, layout_markdown="", schema_json=_SCHEMA
    ).fields[0]
    assert f.bbox == [100, 25, 180, 45]  # span の bbox そのまま
    assert f.page == 1  # span 由来


def test_field_bbox_union_same_page(bundle: PromptBundle) -> None:
    """同一ページの複数 span は外接矩形にまとめる。"""
    resp = _kie_response([{"name": "total_amount", "value": "128000", "span_ids": [10, 11]}])
    adapter = LLMAdapter(FakeProvider([resp]))
    f = kie_extract(
        adapter, bundle, spans=_BOXED, layout_markdown="", schema_json=_SCHEMA
    ).fields[0]
    assert f.bbox == [10, 20, 180, 45]


def test_field_bbox_multipage_dominant_page(bundle: PromptBundle) -> None:
    """ページを跨ぐ根拠は支配ページ（span 数最多）だけを union する。

    跨ぎ union は別画像の座標が混ざった無意味な矩形になる。page も LLM 申告
    ではなく span 由来で上書きする（UI は page でフィルタするため、申告 page が
    ずれていると誤ったページに矩形が出る）。
    """
    spans = [
        Span(span_id=1, page=1, text="a", conf=0.9, bbox=[0, 0, 10, 10]),
        Span(span_id=2, page=2, text="b", conf=0.9, bbox=[50, 50, 60, 60]),
        Span(span_id=3, page=2, text="c", conf=0.9, bbox=[70, 50, 80, 60]),
    ]
    resp = _kie_response(
        [{"name": "total_amount", "value": "x", "span_ids": [1, 2, 3], "page": 1}]
    )
    adapter = LLMAdapter(FakeProvider([resp]))
    f = kie_extract(
        adapter, bundle, spans=spans, layout_markdown="", schema_json=_SCHEMA
    ).fields[0]
    assert f.page == 2  # 申告の 1 ではなく span 数最多のページ
    assert f.bbox == [50, 50, 80, 60]  # 2 ページ目の span のみ


def test_field_bbox_tie_breaks_by_min_page_then_span_id(bundle: PromptBundle) -> None:
    """同数タイは (page, span_id) の辞書順。第一キーが page であること。

    span_id が読み順を表すのは単一 build_spans 呼び出し内だけで、vl_fallback は
    全 OCR ページ確定後に max(span_id)+1 から採番する。1 ページ目が VL・
    2 ページ目が OCR だと 1 ページ目の span_id の方が大きくなるため、
    span_id を第一キーにすると「読み順先頭」の意図と逆のページが選ばれる。
    """
    spans = [
        Span(span_id=5, page=2, text="ocr", conf=0.9, bbox=[0, 0, 10, 10]),  # OCR: 小さい id
        Span(span_id=90, page=1, text="vl", conf=0.9, bbox=[20, 20, 30, 30]),  # VL: 大きい id
    ]
    resp = _kie_response([{"name": "total_amount", "value": "x", "span_ids": [5, 90]}])
    adapter = LLMAdapter(FakeProvider([resp]))
    f = kie_extract(
        adapter, bundle, spans=spans, layout_markdown="", schema_json=_SCHEMA
    ).fields[0]
    assert f.page == 1  # span_id 優先だと 2 になってしまう
    assert f.bbox == [20, 20, 30, 30]


def test_field_bbox_empty_span_ids_keeps_reported_page_no_bbox(bundle: PromptBundle) -> None:
    """根拠 span が無ければ bbox を作らない（原文に無い座標を捏造しない）。"""
    resp = _kie_response(
        [{"name": "total_amount", "value": "999", "span_ids": [999], "page": 3}]
    )
    adapter = LLMAdapter(FakeProvider([resp]))
    f = kie_extract(
        adapter, bundle, spans=_BOXED, layout_markdown="", schema_json=_SCHEMA
    ).fields[0]
    assert f.span_ids == []
    assert f.bbox is None
    assert f.page == 3  # 申告 page は残す（bbox が無いので矛盾しない）
