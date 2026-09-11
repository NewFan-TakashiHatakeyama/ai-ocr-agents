"""kie_extract/llm_correct ノードが llm-adapter で実体化されることの検証（FakeProvider）。"""

import copy
import json

import pytest
from newfan_llm_adapter import FakeProvider, LLMAdapter, PromptBundle, default_bundle_dir
from newfan_schemas import ExtractedField, ReviewStatus, Span

from newfan_orchestrator import llm_nodes, region_hint

_BUNDLE = PromptBundle.load(default_bundle_dir())
_SCHEMA = {
    "doc_type": "invoice",
    "fields": [{"name": "total_amount", "type": "money_jpy", "critical": True}],
}


def test_kie_node_populates_fields() -> None:
    spans = [Span(span_id=11, page=1, text="¥128,000", conf=0.72, bbox=[0, 0, 1, 1])]
    resp = json.dumps(
        {
            "fields": [{"name": "total_amount", "value": "128000", "span_ids": [11], "page": 1}],
            "tables": [],
            "unmapped_required": [],
        }
    )
    adapter = LLMAdapter(FakeProvider([resp]))
    node = llm_nodes.make_kie_extract(adapter, _BUNDLE)
    out = node({"spans": spans, "layout_markdown": "# 請求書", "schema": _SCHEMA})
    assert out["fields"][0].name == "total_amount"
    assert out["fields"][0].span_ids == [11]


def test_correct_node_applies_confusion_pair() -> None:
    # 低確信フィールド。O→0 は混同文字表にある → 自動適用
    span = Span(span_id=1, page=1, text="128,OOO", conf=0.6, bbox=[0, 0, 1, 1])
    field = ExtractedField(
        name="total_amount", value_raw="128,OOO", confidence=0.6, span_ids=[1]
    )
    resp = json.dumps(
        {
            "corrected": "128000",
            "changed": True,
            "needs_review": False,
            "used_pairs": [["O", "0"]],
            "memory_refs": [],
            "rationale": "視覚的に O は 0",
            "confidence": 0.93,
        }
    )
    adapter = LLMAdapter(FakeProvider([resp]))
    node = llm_nodes.make_llm_correct(adapter, _BUNDLE)
    out = node({"fields": [field], "spans": [span], "schema": _SCHEMA})
    f = out["fields"][0]
    assert f.value_normalized == "128000"
    assert f.correction is not None and f.correction["applied"] is True


def test_correct_node_blocks_disallowed_pair() -> None:
    span = Span(span_id=1, page=1, text="128000", conf=0.6, bbox=[0, 0, 1, 1])
    field = ExtractedField(name="total_amount", value_raw="128000", confidence=0.6, span_ids=[1])
    # 1→9 は混同文字表に無い → DD-10 違反 → 適用せず review
    resp = json.dumps(
        {
            "corrected": "198000",
            "changed": True,
            "needs_review": False,
            "used_pairs": [["1", "9"]],
            "memory_refs": [],
            "rationale": "",
            "confidence": 0.9,
        }
    )
    adapter = LLMAdapter(FakeProvider([resp]))
    node = llm_nodes.make_llm_correct(adapter, _BUNDLE)
    out = node({"fields": [field], "spans": [span], "schema": _SCHEMA})
    f = out["fields"][0]
    assert f.value_normalized != "198000"  # 適用されていない
    assert f.review_status is ReviewStatus.PENDING


def test_correct_node_skips_high_confidence() -> None:
    field = ExtractedField(name="total_amount", value_raw="128000", confidence=0.95, span_ids=[1])
    adapter = LLMAdapter(FakeProvider([]))  # 呼ばれないはず
    node = llm_nodes.make_llm_correct(adapter, _BUNDLE)
    out = node({"fields": [field], "spans": [], "schema": _SCHEMA})
    assert out["fields"][0].correction is None


# ---------- region キー除去とプロンプト同一性（設計 §5.6 / §4.7・C27/C29） ----------

_KIE_RESP = json.dumps({"fields": [], "tables": [], "unmapped_required": []})
_SPANS = [Span(span_id=11, page=1, text="¥128,000", conf=0.72, bbox=[0, 0, 1, 1])]
_REGION = {"page": 1, "rect": [0.3, 0.02, 0.72, 0.09]}


def _kie_prompt(schema: dict) -> tuple[str, str]:
    """kie ノードを 1 回動かして、実際に provider へ渡った (system, user) を返す。"""
    provider = FakeProvider([_KIE_RESP])
    node = llm_nodes.make_kie_extract(LLMAdapter(provider), _BUNDLE)
    node({"spans": _SPANS, "layout_markdown": "# 請求書", "schema": schema})
    assert len(provider.calls) == 1
    return provider.calls[0]


def test_kie_prompt_unchanged_without_regions() -> None:
    """region を使わないスキーマのプロンプトは 1 バイトも変わらない。

    gateway と orchestrator-worker のローリング完了順は保証されないため、region を
    知る gateway が先に出て ``"region": null`` を JSONB に書いた版が、region を
    知らない orchestrator に読まれ得る。その場合でもプロンプトが変わらないことを
    **全文一致**で押さえる（部分文字列検索では「どこかが変わった」を見逃す）。
    """
    baseline = _kie_prompt(
        {"doc_type": "invoice", "fields": [{"name": "total_amount", "type": "money_jpy"}]}
    )
    # 旧 gateway 想定入力: region キー自体が無い
    assert _kie_prompt(
        {"doc_type": "invoice", "fields": [{"name": "total_amount", "type": "money_jpy"}]}
    ) == baseline
    # 新 gateway が誤って "region": null を書いてしまった版
    assert _kie_prompt(
        {
            "doc_type": "invoice",
            "fields": [{"name": "total_amount", "type": "money_jpy", "region": None}],
        }
    ) == baseline


def test_region_key_stripped_from_schema_prompt() -> None:
    """実座標が設定された版でも、Phase 1 ではプロンプトに載せない。

    region は正規化座標（0.30 等）であり、素通しすると LLM に意味不明な数値が
    渡る。プロンプトへのヒント注入は Phase 4 で画素へ射影した形として別途設計・
    計測する。
    """
    baseline_system, baseline_user = _kie_prompt(
        {"doc_type": "invoice", "fields": [{"name": "total_amount", "type": "money_jpy"}]}
    )
    system, user = _kie_prompt(
        {
            "doc_type": "invoice",
            "fields": [{"name": "total_amount", "type": "money_jpy", "region": _REGION}],
        }
    )
    assert (system, user) == (baseline_system, baseline_user)
    # プロンプトへ埋まる schema JSON そのものに座標が残っていないこと
    # （"region" は kie テンプレート本文にも現れ得るので、user 全文の
    #   部分文字列検索では判定できない）
    schema_json = json.dumps(
        llm_nodes._schema_for_prompt(
            {
                "doc_type": "invoice",
                "fields": [{"name": "total_amount", "type": "money_jpy", "region": _REGION}],
            }
        ),
        ensure_ascii=False,
    )
    assert "region" not in schema_json and "0.72" not in schema_json


def test_state_schema_not_mutated() -> None:
    """state の schema を破壊しない。

    LangGraph の state は他ノードと共有され checkpoint にも載る。ここで書き換えると
    HITL 再開時の入力が変わり、再現しないバグになる。
    """
    schema = {
        "doc_type": "invoice",
        "fields": [{"name": "total_amount", "type": "money_jpy", "region": _REGION}],
    }
    before = copy.deepcopy(schema)
    node = llm_nodes.make_kie_extract(LLMAdapter(FakeProvider([_KIE_RESP])), _BUNDLE)
    node({"spans": _SPANS, "layout_markdown": "", "schema": schema})
    assert schema == before


def test_schema_for_prompt_handles_malformed_fields() -> None:
    """fields が list でない / 要素が dict でない版でも落ちない（fail-open）。

    field_schemas.fields は JSONB で、過去の書き込みや手動修正で形が崩れ得る。
    ここで例外を投げるとノードごと落ち、worker が ACK しないまま再配信ループに入る。
    """
    assert llm_nodes._schema_for_prompt({"doc_type": "x", "fields": None}) == {
        "doc_type": "x",
        "fields": None,
    }
    out = llm_nodes._schema_for_prompt({"doc_type": "x", "fields": ["junk", {"name": "a"}]})
    assert out["fields"] == ["junk", {"name": "a"}]


# ---------- 読取領域を KIE ヒントとして渡す（既定 off・計測ゲート付き） ----------
#
# 設計 region-field-add-and-hint-v2 §2.5〜§2.7 / D13〜D16。ヒントは矩形ではなく
# 「矩形の中にある span の候補列」として渡し、渡す前に決定論で落とす。

_PAGES_1 = [{"page_no": 1, "width": 1000, "height": 2000}]
_PAGES_3 = [
    {"page_no": 1, "width": 1000, "height": 2000},
    {"page_no": 2, "width": 1000, "height": 2000},
    {"page_no": 3, "width": 800, "height": 1600},
]
# rect [0.3, 0.1, 0.7, 0.2] × 1000x2000 → 画素 [300, 200, 700, 400]
_RECT_PX = [300, 200, 700, 400]
_SCHEMA_REGION = {
    "doc_type": "invoice",
    "fields": [
        {"name": "total_amount", "type": "money_jpy", "region": {"page": 1, "rect": [0.3, 0.1, 0.7, 0.2]}},
        {"name": "memo", "type": "string"},
    ],
}


def _sp(span_id: int, text: str, bbox: list[int], page: int = 1) -> Span:
    return Span(span_id=span_id, page=page, text=text, conf=0.9, bbox=bbox)


def _field_schema(name: str, ftype: str, example: str | None = None) -> dict:
    region: dict = {"page": 1, "rect": [0.3, 0.1, 0.7, 0.2]}
    if example is not None:
        region["example_value"] = example
    return {"doc_type": "invoice", "fields": [{"name": name, "type": ftype, "region": region}]}


_IN = _sp(11, "¥128,000", [320, 220, 480, 260])  # 矩形の中
_OUT = _sp(12, "備考", [10, 1500, 200, 1540])  # 矩形の外


def test_ヒント既定offならプロンプトは現行と完全一致(monkeypatch) -> None:
    """設計の約束は「精度改善を実測できた場合のみ出荷」。既定では領域を持つ
    スキーマでも現行と 1 バイトも変わらず、metrics にも触らないこと。
    """
    monkeypatch.delenv("REGION_KIE_HINTS", raising=False)
    baseline = _kie_prompt(
        {"doc_type": "invoice", "fields": [
            {"name": "total_amount", "type": "money_jpy"}, {"name": "memo", "type": "string"}]}
    )
    assert llm_nodes._schema_for_prompt(_SCHEMA_REGION, _PAGES_1, [_IN])["fields"][0].keys() == {
        "name", "type"
    }
    provider = FakeProvider([_KIE_RESP])
    out = llm_nodes.make_kie_extract(LLMAdapter(provider), _BUNDLE)(
        {"spans": _SPANS, "layout_markdown": "# 請求書", "schema": _SCHEMA_REGION, "pages": _PAGES_1,
         "metrics": {"region": {"excluded_spans": 1}}}
    )
    assert provider.calls[0] == baseline
    assert "metrics" not in out  # 既存の metrics を一切変えない


def test_spans_を省略した呼び出しではヒントを付けない(monkeypatch) -> None:
    """旧来の ``_schema_for_prompt(schema, pages)`` 互換。候補を作れないのに
    region だけ落とすのが正しい。"""
    monkeypatch.setenv("REGION_KIE_HINTS", "1")
    out = llm_nodes._schema_for_prompt(_SCHEMA_REGION, _PAGES_1)
    assert out["fields"][0] == {"name": "total_amount", "type": "money_jpy"}


def test_ヒント有効時は候補_span_を_region_hint_として載せる(monkeypatch) -> None:
    monkeypatch.setenv("REGION_KIE_HINTS", "1")
    out, report = llm_nodes.build_region_hints(_SCHEMA_REGION, _PAGES_1, [_IN, _OUT])
    assert out["fields"][0]["region_hint"] == {
        "candidates": [{"span_id": 11, "text": "¥128,000"}],
        "example_value": None,
        "example_present": False,
    }
    assert "region" not in out["fields"][0], "正規化座標をそのまま渡さない"
    assert "region_px" not in out["fields"][0], "矩形の注入（旧方式）は廃止"
    # 領域を持たない項目には何も足さない
    assert out["fields"][1] == {"name": "memo", "type": "string"}
    assert report.given == ["total_amount"] and report.dropped == {} and report.truncated == {}


def test_例示値と_example_present(monkeypatch) -> None:
    """照合は D18 の norm_key（敬称・全角半角・記号の揺れを同一視）。"""
    monkeypatch.setenv("REGION_KIE_HINTS", "1")
    schema = _field_schema("customer_name", "string", "株式会社千曲川ホーム")
    out, _ = llm_nodes.build_region_hints(
        schema, _PAGES_1, [_sp(1, "株式会社千曲川ホーム 御中", [320, 220, 480, 260])]
    )
    hint = out["fields"][0]["region_hint"]
    assert hint["example_value"] == "株式会社千曲川ホーム"
    assert hint["example_present"] is True

    out2, _ = llm_nodes.build_region_hints(schema, _PAGES_1, [_sp(1, "株式会社山田", [320, 220, 480, 260])])
    assert out2["fields"][0]["region_hint"]["example_present"] is False


def test_例示値は渡す直前にも消毒する(monkeypatch) -> None:
    """JSONB は検査導入前のデータや手修正で保存時の規則を素通りし得る。LLM に渡す
    文字列なので、保存時と同じ規則（制御文字除去・200 字）をここでも当てる。"""
    monkeypatch.setenv("REGION_KIE_HINTS", "1")
    schema = _field_schema("customer_name", "string", "株式\x00会社\n千曲川ホーム" + "x" * 500)
    out, _ = llm_nodes.build_region_hints(schema, _PAGES_1, [_sp(1, "何か", [320, 220, 480, 260])])
    ev = out["fields"][0]["region_hint"]["example_value"]
    assert ev.startswith("株式会社千曲川ホーム") and len(ev) == 200
    # 消毒して何も残らなければ「例示値なし」
    schema2 = _field_schema("customer_name", "string", "\x00\x07")
    out2, _ = llm_nodes.build_region_hints(schema2, _PAGES_1, [_sp(1, "何か", [320, 220, 480, 260])])
    assert out2["fields"][0]["region_hint"]["example_value"] is None


@pytest.mark.parametrize(
    ("bbox", "page", "expected"),
    [
        ([320, 220, 480, 260], 1, True),  # 完全に中
        ([250, 190, 350, 210], 1, True),  # 中心点 (300, 200) が境界上 → 含む
        ([90, 250, 500, 350], 1, True),  # 中心点は外だが重なりが span 面積の 48%
        ([90, 250, 390, 350], 1, True),  # 重なりがちょうど 30% → 含む（境界は含む側）
        ([0, 250, 420, 350], 1, False),  # 中心点は外・重なり 28.6% → 含まない
        ([0, 1500, 200, 1540], 1, False),  # 完全に外
        ([320, 220, 480, 260], 2, False),  # 別ページ（座標は中）
    ],
)
def test_候補の判定は中心点または重なり30パーセント(monkeypatch, bbox, page, expected) -> None:
    """除外の _covered（50%）は流用しない。人が引いた枠は文字の一部にしか掛からないのが
    普通で、拾い漏れると 0 件→ヒントごと落ちる（設計 §2.5 / R5）。"""
    monkeypatch.setenv("REGION_KIE_HINTS", "1")
    schema = _field_schema("title", "string")
    out, report = llm_nodes.build_region_hints(schema, _PAGES_1, [_sp(1, "請求書", bbox, page)])
    assert ("region_hint" in out["fields"][0]) is expected
    if not expected:
        assert report.dropped == {"title": "no_spans_in_region"}


def test_順位は重なりが大きい順で12件で切り切った件数を残す(monkeypatch) -> None:
    """読み順で切ると、大きな枠で本命が後ろに来たとき落ちる（R17）。"""
    monkeypatch.setenv("REGION_KIE_HINTS", "1")
    # 読み順（span_id 昇順）ほど重なりが小さい 14 件。本命は最後（最大）。
    spans = [_sp(i, f"t{i}", [310, 210 + i * 12, 310 + 20 * (i + 1), 220 + i * 12]) for i in range(14)]
    out, report = llm_nodes.build_region_hints(_field_schema("title", "string"), _PAGES_1, spans)
    cands = out["fields"][0]["region_hint"]["candidates"]
    assert len(cands) == region_hint.HINT_MAX_CANDIDATES == 12
    assert cands[0] == {"span_id": 13, "text": "t13"}  # 重なり最大が先頭
    assert [c["span_id"] for c in cands] == list(range(13, 1, -1))  # 0 と 1 が切られた
    assert report.truncated == {"title": 2}
    assert report.given == ["title"]


def test_no_spans_in_region(monkeypatch) -> None:
    """この帳票ではその位置に何も無い（レイアウト違い）。ヒントごと落とす。"""
    monkeypatch.setenv("REGION_KIE_HINTS", "1")
    out, report = llm_nodes.build_region_hints(_SCHEMA_REGION, _PAGES_1, [_OUT])
    assert "region_hint" not in out["fields"][0] and "region" not in out["fields"][0]
    assert report.dropped == {"total_amount": "no_spans_in_region"}
    assert report.given == []


def test_overlaps_exclude(monkeypatch) -> None:
    """候補 0 件で、矩形が適用済み除外領域と重なる → 作者が矛盾した領域を引いている。
    no_spans と区別して伝える。"""
    monkeypatch.setenv("REGION_KIE_HINTS", "1")
    _, report = llm_nodes.build_region_hints(
        _SCHEMA_REGION, _PAGES_1, [_OUT], {1: [[350, 250, 400, 300]]}
    )
    assert report.dropped == {"total_amount": "overlaps_exclude"}
    # 重ならない除外領域なら no_spans のまま
    _, report2 = llm_nodes.build_region_hints(
        _SCHEMA_REGION, _PAGES_1, [_OUT], {1: [[0, 1500, 100, 1600]]}
    )
    assert report2.dropped == {"total_amount": "no_spans_in_region"}
    # 候補があるなら除外と重なっていても落とさない（候補は除外適用後の span）
    out3, report3 = llm_nodes.build_region_hints(
        _SCHEMA_REGION, _PAGES_1, [_IN], {1: [[350, 250, 400, 300]]}
    )
    assert "region_hint" in out3["fields"][0] and report3.dropped == {}


def test_type_mismatch_割れた日付は落ちない(monkeypatch) -> None:
    """OCR は「令和」「5年」「5月」「1日」に割ることがある。単体で見ると全部落ちるので
    連結にも当てる（R6）。"""
    monkeypatch.setenv("REGION_KIE_HINTS", "1")
    spans = [
        _sp(1, "令和", [310, 210, 350, 240]),
        _sp(2, "5年", [360, 210, 390, 240]),
        _sp(3, "5月", [400, 210, 430, 240]),
        _sp(4, "1日", [440, 210, 470, 240]),
    ]
    out, report = llm_nodes.build_region_hints(_field_schema("issue_date", "date"), _PAGES_1, spans)
    assert report.dropped == {}
    assert [c["text"] for c in out["fields"][0]["region_hint"]["candidates"]] == ["令和", "5年", "5月", "1日"]


def test_type_mismatch_数字の無い金額欄は落ちる(monkeypatch) -> None:
    monkeypatch.setenv("REGION_KIE_HINTS", "1")
    out, report = llm_nodes.build_region_hints(
        _SCHEMA_REGION, _PAGES_1, [_sp(1, "合計金額", [320, 220, 480, 260]), _sp(2, "税込", [500, 220, 560, 260])]
    )
    assert report.dropped == {"total_amount": "type_mismatch"}
    assert "region_hint" not in out["fields"][0]


@pytest.mark.parametrize(
    ("ftype", "texts", "dropped"),
    [
        ("money_jpy", ["¥128,000"], False),
        ("money_jpy", ["御請求金額", "128,000"], False),
        ("number", ["3個"], False),
        ("number", ["個"], True),
        ("jp_invoice_reg_no", ["T", "1234567890123"], False),  # 連結で 13 桁
        ("jp_invoice_reg_no", ["株式会社千曲川ホーム"], True),
        ("tax_rate_jp", ["10%"], False),
        ("tax_rate_jp", ["軽減"], True),
        ("date", ["2024/05/01"], False),
        # 年省略は落とす側に倒れる（文脈年を渡すと '10.5' '3-1' が日付として通る）
        ("date", ["5月31日"], True),
        ("date", ["10.5"], True),
        # 数値系は数字を 1 つも含まなければ落とす（norm_money_jpy は '.' 入りを素通しする）
        ("money_jpy", ["………"], True),
        ("money_jpy", ["No."], True),
        ("money_jpy", ["Co., Ltd."], True),
        ("date", ["大熊邸"], True),
        ("string", ["なんでも"], False),  # 型無しは判定しない
    ],
)
def test_type_mismatch_の型ごとの解釈(monkeypatch, ftype, texts, dropped) -> None:
    """型の解釈は §5.6 の正規化器をそのまま使う（新しくパーサを書かない）。
    date / jp_invoice_reg_no は正規化器が None を返さない型なので出力の形で判定する。"""
    monkeypatch.setenv("REGION_KIE_HINTS", "1")
    spans = [_sp(i + 1, t, [310 + i * 60, 210, 360 + i * 60, 240]) for i, t in enumerate(texts)]
    _, report = llm_nodes.build_region_hints(_field_schema("f", ftype), _PAGES_1, spans)
    assert (report.dropped.get("f") == "type_mismatch") is dropped, report.dropped


def test_kind_conflict_会社名の項目に建物名は落ちる(monkeypatch) -> None:
    """sample8: customer_name の領域が 1 行ずれて「大熊邸」を指した。座標だけでは
    正しいヒントに見えるが、例示値「株式会社千曲川ホーム」と種類が違う。"""
    monkeypatch.setenv("REGION_KIE_HINTS", "1")
    schema = _field_schema("customer_name", "string", "株式会社千曲川ホーム")
    out, report = llm_nodes.build_region_hints(schema, _PAGES_1, [_sp(1, "大熊邸", [320, 220, 480, 260])])
    assert report.dropped == {"customer_name": "kind_conflict"}
    assert "region_hint" not in out["fields"][0]


def test_kind_conflict_company_と_person_は落とさない(monkeypatch) -> None:
    """宛先が会社のことも個人のこともある。区別が難しく、誤って落とすと効き目を失う。"""
    monkeypatch.setenv("REGION_KIE_HINTS", "1")
    schema = _field_schema("customer_name", "string", "株式会社千曲川ホーム")
    out, report = llm_nodes.build_region_hints(schema, _PAGES_1, [_sp(1, "大熊 和一", [320, 220, 480, 260])])
    assert report.dropped == {} and "region_hint" in out["fields"][0]
    # 逆向きも同じ
    schema2 = _field_schema("customer_name", "string", "大熊 和一")
    out2, report2 = llm_nodes.build_region_hints(
        schema2, _PAGES_1, [_sp(1, "株式会社千曲川ホーム", [320, 220, 480, 260])]
    )
    assert report2.dropped == {} and "region_hint" in out2["fields"][0]


def test_kind_conflict_unknown_は落とさない(monkeypatch) -> None:
    monkeypatch.setenv("REGION_KIE_HINTS", "1")
    schema = _field_schema("customer_name", "string", "株式会社千曲川ホーム")
    _, report = llm_nodes.build_region_hints(schema, _PAGES_1, [_sp(1, "ご担当", [320, 220, 480, 260])])
    assert report.dropped == {}
    # 例示値が unknown でも落とさない
    schema2 = _field_schema("customer_name", "string", "ご担当")
    _, report2 = llm_nodes.build_region_hints(schema2, _PAGES_1, [_sp(1, "大熊邸", [320, 220, 480, 260])])
    assert report2.dropped == {}
    # 例示値が無ければ種類は見ない
    schema3 = _field_schema("customer_name", "string")
    _, report3 = llm_nodes.build_region_hints(schema3, _PAGES_1, [_sp(1, "大熊邸", [320, 220, 480, 260])])
    assert report3.dropped == {}


def test_kind_conflict_同じ種類の候補が1つでもあれば落とさない(monkeypatch) -> None:
    monkeypatch.setenv("REGION_KIE_HINTS", "1")
    schema = _field_schema("customer_name", "string", "株式会社千曲川ホーム")
    _, report = llm_nodes.build_region_hints(
        schema, _PAGES_1,
        [_sp(1, "大熊邸", [320, 220, 480, 260]), _sp(2, "株式会社山田", [320, 270, 480, 300])],
    )
    assert report.dropped == {}


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("395,217", "amount"),
        ("¥128,000", "amount"),
        ("58,300円", "amount"),
        ("１２８，０００円", "amount"),
        ("令和5年5月1日", "date"),
        ("2024/05/01", "date"),
        ("2024年5月", "date"),
        ("T1234567890123", "id"),
        ("INV-2024-001", "id"),
        ("株式会社千曲川ホーム", "company"),
        ("株式会社町田製作所", "company"),  # 社名に「町」→ address ではなく company
        ("㈱山田", "company"),
        ("Yamada Co., Ltd.", "company"),
        ("東京都新宿区高田馬場XX-X", "address"),
        ("長野県千曲市大字戸倉1234", "address"),
        ("美しが丘18丁目5番地2号", "address"),
        ("大熊邸", "building"),
        ("新宿ビル", "building"),
        ("大熊 和一", "person"),
        ("大熊和一 様", "person"),
        ("中村 一郎", "person"),  # 「村」1 文字では住所にしない
        ("令和", "unknown"),
        ("5年", "unknown"),
        ("合計", "unknown"),
        ("", "unknown"),
        (None, "unknown"),
    ],
)
def test_種類判定の表(text, kind) -> None:
    assert region_hint.classify_kind(text) == kind


def test_last_は総ページ数へ解決しページごとの寸法を使う(monkeypatch) -> None:
    monkeypatch.setenv("REGION_KIE_HINTS", "1")
    schema = {
        "doc_type": "invoice",
        "fields": [{"name": "total", "type": "money_jpy",
                    "region": {"page": "last", "rect": [0.5, 0.5, 1.0, 0.75]}}],
    }
    # p3 は 800x1600 → 画素 [400, 800, 800, 1200]
    inside = _sp(1, "¥1,000", [420, 820, 500, 850], page=3)
    same_xy_on_p1 = _sp(2, "¥2,000", [420, 820, 500, 850], page=1)
    out, report = llm_nodes.build_region_hints(schema, _PAGES_3, [inside, same_xy_on_p1])
    assert out["fields"][0]["region_hint"]["candidates"] == [{"span_id": 1, "text": "¥1,000"}]
    assert report.given == ["total"]


def test_存在しないページを指す領域はヒントごと落とす(monkeypatch) -> None:
    """1 ページ目へ縮退させると、まったく違う場所を指すヒントになり誤誘導になる。"""
    monkeypatch.setenv("REGION_KIE_HINTS", "1")
    schema = {
        "doc_type": "invoice",
        "fields": [{"name": "total", "type": "money_jpy",
                    "region": {"page": 9, "rect": [0.1, 0.1, 0.2, 0.2]}}],
    }
    out, report = llm_nodes.build_region_hints(schema, _PAGES_1, [_IN])
    assert "region_hint" not in out["fields"][0]
    assert "region" not in out["fields"][0]
    assert report.dropped == {"total": "page_out_of_range"}


def test_寸法の無いページの領域はヒントごと落とす(monkeypatch) -> None:
    monkeypatch.setenv("REGION_KIE_HINTS", "1")
    out, report = llm_nodes.build_region_hints(
        _SCHEMA_REGION, [{"page_no": 1, "image_uri": "x"}], [_IN]
    )
    assert "region_hint" not in out["fields"][0]
    assert report.dropped == {"total_amount": "page_unprojectable"}


def test_ヒント有効でも_span_に座標は載せない(monkeypatch) -> None:
    """ヒントは候補 span の id と原文で渡す（D13）。全 span への bbox 付与は廃止。"""
    monkeypatch.setenv("REGION_KIE_HINTS", "1")
    provider = FakeProvider([_KIE_RESP])
    llm_nodes.make_kie_extract(LLMAdapter(provider), _BUNDLE)(
        {"spans": [_IN], "layout_markdown": "", "schema": _SCHEMA_REGION, "pages": _PAGES_1}
    )
    user = provider.calls[0][1]
    assert '"region_hint"' in user and '"candidates"' in user
    assert '"bbox"' not in user and '"region_px"' not in user
    # ヒントの説明文は末尾に足されている（空間的な語は使わない）
    assert "ヒントを捨てる基準" in user
    for word in ("近く", "付近"):
        assert word not in _BUNDLE.kie_region_hint_template


def test_metrics_に理由と結果が残り再実行で書き直され既存の_region_を潰さない(monkeypatch) -> None:
    """metrics は LastValue チャネル。既存の region（excluded_* 等）を読んでから返し、
    hints は run ごとに丸ごと書き直す（前回実行の残骸を残さない）。"""
    monkeypatch.setenv("REGION_KIE_HINTS", "1")
    schema = {
        "doc_type": "invoice",
        "fields": [
            {"name": "total_amount", "type": "money_jpy", "region": {"page": 1, "rect": [0.3, 0.1, 0.7, 0.2]}},
            {"name": "customer_name", "type": "string",
             "region": {"page": 1, "rect": [0.3, 0.5, 0.7, 0.6], "example_value": "株式会社千曲川ホーム"}},
            {"name": "issue_date", "type": "date", "region": {"page": 1, "rect": [0.3, 0.8, 0.7, 0.9]}},
        ],
    }
    spans = [
        _IN,  # total_amount の候補
        _sp(21, "大熊邸", [320, 1020, 480, 1060]),  # customer_name: kind_conflict
        # issue_date の領域 [300,1600,700,1800] には何も無い
    ]
    resp = json.dumps({
        "fields": [{"name": "total_amount", "value": "128000", "span_ids": [11], "page": 1}],
        "tables": [], "unmapped_required": [],
    })
    out = llm_nodes.make_kie_extract(LLMAdapter(FakeProvider([resp])), _BUNDLE)(
        {
            "spans": spans, "layout_markdown": "", "schema": schema, "pages": _PAGES_1,
            "metrics": {
                "region": {
                    "excluded_spans": 3,
                    "mismatch_fields": ["x"],
                    "hints": {"given": ["stale"], "dropped": {"stale2": "no_spans_in_region"},
                              "truncated": {"stale": 9}, "outcomes": {"stale": "followed"}},
                },
                "other": 1,
            },
        }
    )
    region = out["metrics"]["region"]
    assert region["excluded_spans"] == 3 and region["mismatch_fields"] == ["x"]
    assert out["metrics"]["other"] == 1
    assert region["hints"] == {
        "given": ["total_amount"],
        "dropped": {"customer_name": "kind_conflict", "issue_date": "no_spans_in_region"},
        "truncated": {},
        "outcomes": {"total_amount": "followed"},
        "detail": {
            "total_amount": {"example_value": None, "candidates": ["¥128,000"]},
            "customer_name": {"example_value": "株式会社千曲川ホーム", "candidates": ["大熊邸"]},
        },
    }


def test_除外領域と重なる読取領域は_overlaps_exclude_として_metrics_に残る(monkeypatch) -> None:
    """除外の画素矩形は state の exclude_regions から同じ規則で引き直す。"""
    monkeypatch.setenv("REGION_KIE_HINTS", "1")
    out = llm_nodes.make_kie_extract(LLMAdapter(FakeProvider([_KIE_RESP])), _BUNDLE)(
        {
            "spans": [_OUT], "layout_markdown": "", "schema": _SCHEMA_REGION, "pages": _PAGES_1,
            "exclude_regions": [{"page": None, "rect": [0.35, 0.12, 0.40, 0.15], "label": "社印"}],
        }
    )
    assert out["metrics"]["region"]["hints"]["dropped"] == {"total_amount": "overlaps_exclude"}


def test_ヒント有効でも領域なしスキーマでは_metrics_を書かない(monkeypatch) -> None:
    monkeypatch.setenv("REGION_KIE_HINTS", "1")
    out = llm_nodes.make_kie_extract(LLMAdapter(FakeProvider([_KIE_RESP])), _BUNDLE)(
        {"spans": [_IN], "layout_markdown": "",
         "schema": {"doc_type": "invoice", "fields": [{"name": "memo", "type": "string"}]},
         "pages": _PAGES_1, "metrics": {"region": {"excluded_spans": 1}}}
    )
    assert "metrics" not in out


def test_outcomes_は_span_ids_と候補の集合演算で決まる(monkeypatch) -> None:
    monkeypatch.setenv("REGION_KIE_HINTS", "1")
    spans = [_IN, _sp(13, "¥99,000", [320, 300, 480, 340]), _sp(14, "¥1", [10, 1500, 100, 1540])]

    def _run(span_ids: list[int]) -> str:
        resp = json.dumps({
            "fields": [{"name": "total_amount", "value": "x", "span_ids": span_ids, "page": 1}],
            "tables": [], "unmapped_required": [],
        })
        out = llm_nodes.make_kie_extract(LLMAdapter(FakeProvider([resp])), _BUNDLE)(
            {"spans": spans, "layout_markdown": "", "schema": _SCHEMA_REGION, "pages": _PAGES_1}
        )
        return out["metrics"]["region"]["hints"]["outcomes"]["total_amount"]

    assert _run([11]) == "followed"
    assert _run([11, 13]) == "followed"
    assert _run([11, 14]) == "partial"
    assert _run([14]) == "rejected"
    assert _run([]) == "no_evidence"  # 根拠なしは rejected と区別する（§2.9 の集計母集団）


def test_領域を使わないプロンプトはスナップショットと完全一致(monkeypatch) -> None:
    """**コミット済みの実文字列**と突き合わせる。

    以前の不変テストは「現行コードを 2 回呼んで比べる」形だったので、プロンプト
    テンプレート自体を書き換えても常に緑だった。実際に Phase 4 の作業で
    kie_extract.yaml へヒント文を足してしまい、ヒント off でも全テナントのプロンプトが
    1016 バイト変わった状態に気付けなかった。テンプレートは全 KIE 呼び出しが読むので、
    領域を 1 つも使っていないテナントまで巻き込む。外部の固定値で縛る。
    """
    import pathlib

    monkeypatch.delenv("REGION_KIE_HINTS", raising=False)
    snap = pathlib.Path(__file__).parent / "snapshots" / "kie_prompt_no_region.txt"
    want_system, want_user = snap.read_text(encoding="utf-8").split("\n---8<---\n", 1)

    provider = FakeProvider([_KIE_RESP])
    llm_nodes.make_kie_extract(LLMAdapter(provider), _BUNDLE)(
        {
            "spans": [Span(span_id=11, page=1, text="¥128,000", conf=0.72, bbox=[0, 0, 1, 1])],
            "layout_markdown": "# 請求書",
            "schema": {"doc_type": "invoice", "fields": [{"name": "total_amount", "type": "money_jpy"}]},
            "pages": [{"page_no": 1, "width": 1000, "height": 2000}],
        }
    )
    got_system, got_user = provider.calls[0]
    assert got_system == want_system
    assert got_user == want_user, "領域を使わない run のプロンプトが変わっている"


def test_ヒント有効でも領域なしスキーマのプロンプトは変わらない(monkeypatch) -> None:
    """フラグを立てても、領域を持たないスキーマには何も足さない。"""
    import pathlib

    monkeypatch.setenv("REGION_KIE_HINTS", "1")
    snap = pathlib.Path(__file__).parent / "snapshots" / "kie_prompt_no_region.txt"
    _, want_user = snap.read_text(encoding="utf-8").split("\n---8<---\n", 1)

    provider = FakeProvider([_KIE_RESP])
    llm_nodes.make_kie_extract(LLMAdapter(provider), _BUNDLE)(
        {
            "spans": [Span(span_id=11, page=1, text="¥128,000", conf=0.72, bbox=[0, 0, 1, 1])],
            "layout_markdown": "# 請求書",
            "schema": {"doc_type": "invoice", "fields": [{"name": "total_amount", "type": "money_jpy"}]},
            "pages": [{"page_no": 1, "width": 1000, "height": 2000}],
        }
    )
    assert provider.calls[0][1] == want_user


def test_明細フィールドにはヒントを注入しない(monkeypatch) -> None:
    """行数が増えたり次ページへ続いた帳票で「領域に近い行だけ」を選ばせると、
    行が静かに切り捨てられる。位置ガードは TableResult を見ないので気付けない。
    """
    monkeypatch.setenv("REGION_KIE_HINTS", "1")
    schema = {
        "doc_type": "invoice",
        "fields": [
            {
                "name": "line_items",
                "type": "table",
                "columns": [{"name": "item", "type": "string"}],
                "region": {"page": 1, "rect": [0.1, 0.4, 0.9, 0.8]},
            }
        ],
    }
    out, report = llm_nodes.build_region_hints(
        schema, [{"page_no": 1, "width": 1000, "height": 2000}], [_sp(1, "品名", [200, 900, 300, 940])]
    )
    assert "region_hint" not in out["fields"][0]
    assert "region_px" not in out["fields"][0]
    assert "region" not in out["fields"][0]
    assert out["fields"][0]["columns"] == [{"name": "item", "type": "string"}]
    assert not report  # 評価すらしない＝metrics にも載らない


# ---------- 敵対的レビュー Phase B の major に対する回帰 ----------


def test_順位は_span_面積ではなく矩形との重なりで決まる(monkeypatch) -> None:
    """レビューで指摘された変異: rank_candidates を span 面積順にしても既存テストは通った。

    大きい span が矩形に少ししか掛かっていない一方、小さい span が矩形の中に丸ごと
    入っているとき、後者が先頭に来ること（重なり面積順）を固定する。
    """
    monkeypatch.setenv("REGION_KIE_HINTS", "1")
    # 矩形は [300,200,700,400]。tall は中心が矩形内で候補になるが、縦に長く大半が外
    tall = _sp(20, "tall", [400, 0, 600, 600])  # 面積 120,000、重なり 200×200 = 40,000
    inside = _sp(21, "inside", [310, 210, 690, 390])  # 面積 68,400、重なり 68,400（全部）
    tiny = _sp(22, "tiny", [330, 300, 380, 320])  # 面積 1,000、重なり 1,000
    out, _ = llm_nodes.build_region_hints(_field_schema("title", "string"), _PAGES_1, [tall, tiny, inside])
    ids = [c["span_id"] for c in out["fields"][0]["region_hint"]["candidates"]]
    # 面積順なら tall が先頭、重なり順なら inside が先頭。読み順なら tall → tiny → inside
    assert ids == [21, 20, 22]


def test_分割された日付と見出し語が同じ枠にあっても_kind_conflict_で落ちない(monkeypatch) -> None:
    """major: 候補を 1 span ずつ分類すると「発行日」（漢字の見出し語）が person、
    「令和」「5年」「5月」「1日」は単体では date にならず、例示値が日付だと kind_conflict で
    落ちていた。連結を見れば date になる。type_mismatch 側と同じ扱いにする。
    """
    monkeypatch.setenv("REGION_KIE_HINTS", "1")
    spans = [
        _sp(30, "発行日", [310, 210, 380, 240]),
        _sp(31, "令和", [400, 210, 440, 240]),
        _sp(32, "5年", [445, 210, 480, 240]),
        _sp(33, "5月", [485, 210, 520, 240]),
        _sp(34, "1日", [525, 210, 560, 240]),
    ]
    schema = _field_schema("document_date", "date", example="令和4年12月31日")
    out, report = llm_nodes.build_region_hints(schema, _PAGES_1, spans)
    assert report.dropped == {}, report.dropped
    assert "region_hint" in out["fields"][0]


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("発行日", "unknown"),  # 見出し語は person にしない
        ("合計金額", "unknown"),
        ("御請求先", "unknown"),
        ("山田工務店", "unknown"),  # 法人格の無い屋号
        ("大熊 和一", "person"),  # 姓 名（空白あり）
        ("大熊和一様", "person"),  # 末尾の様
        ("大熊邸", "building"),
    ],
)
def test_person_は空白区切りか末尾の様だけ(text: str, kind: str) -> None:
    from newfan_orchestrator.region_hint import classify_kind

    assert classify_kind(text) == kind


def test_例示値が複数spanの連結でも_example_present_になる(monkeypatch) -> None:
    """手描きの例示値は「枠の下の span を読み順で連結」（D11）。候補 1 つずつと比べると
    同じ紙面でも一致しない（minor）。連結にも当てる。"""
    monkeypatch.setenv("REGION_KIE_HINTS", "1")
    spans = [
        _sp(40, "株式会社", [310, 210, 400, 240]),
        _sp(41, "千曲川ホーム", [405, 210, 560, 240]),
    ]
    schema = _field_schema("customer_name", "string", example="株式会社 千曲川ホーム")
    out, _ = llm_nodes.build_region_hints(schema, _PAGES_1, spans)
    assert out["fields"][0]["region_hint"]["example_present"] is True
