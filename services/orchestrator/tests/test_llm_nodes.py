"""kie_extract/llm_correct ノードが llm-adapter で実体化されることの検証（FakeProvider）。"""

import copy
import json

from newfan_llm_adapter import FakeProvider, LLMAdapter, PromptBundle, default_bundle_dir
from newfan_schemas import ExtractedField, ReviewStatus, Span

from newfan_orchestrator import llm_nodes

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


# ---------- Phase 4: 読取領域を KIE ヒントとして渡す（既定 off・計測ゲート付き） ----------

_PAGES_1 = [{"page_no": 1, "width": 1000, "height": 2000}]
_PAGES_3 = [
    {"page_no": 1, "width": 1000, "height": 2000},
    {"page_no": 2, "width": 1000, "height": 2000},
    {"page_no": 3, "width": 800, "height": 1600},
]
_SCHEMA_REGION = {
    "doc_type": "invoice",
    "fields": [
        {"name": "total_amount", "type": "money_jpy", "region": {"page": 1, "rect": [0.3, 0.1, 0.7, 0.2]}},
        {"name": "memo", "type": "string"},
    ],
}


def test_ヒント既定offならプロンプトは現行と完全一致(monkeypatch) -> None:
    """設計の約束は「精度改善を実測できた場合のみ出荷」。既定では領域を持つ
    スキーマでも現行と 1 バイトも変わらないこと。
    """
    monkeypatch.delenv("REGION_KIE_HINTS", raising=False)
    baseline = _kie_prompt(
        {"doc_type": "invoice", "fields": [
            {"name": "total_amount", "type": "money_jpy"}, {"name": "memo", "type": "string"}]}
    )
    assert llm_nodes._schema_for_prompt(_SCHEMA_REGION, _PAGES_1)["fields"][0].keys() == {
        "name", "type"
    }
    provider = FakeProvider([_KIE_RESP])
    llm_nodes.make_kie_extract(LLMAdapter(provider), _BUNDLE)(
        {"spans": _SPANS, "layout_markdown": "# 請求書", "schema": _SCHEMA_REGION, "pages": _PAGES_1}
    )
    assert provider.calls[0] == baseline


def test_ヒント有効時は画素へ射影した_region_px_を載せる(monkeypatch) -> None:
    monkeypatch.setenv("REGION_KIE_HINTS", "1")
    out = llm_nodes._schema_for_prompt(_SCHEMA_REGION, _PAGES_1)
    assert out["fields"][0]["region_px"] == {"page": 1, "bbox": [300, 200, 700, 400]}
    assert "region" not in out["fields"][0], "正規化座標をそのまま渡さない"
    # 領域を持たない項目には何も足さない
    assert out["fields"][1] == {"name": "memo", "type": "string"}


def test_last_は総ページ数へ解決しページごとの寸法を使う(monkeypatch) -> None:
    monkeypatch.setenv("REGION_KIE_HINTS", "1")
    schema = {
        "doc_type": "invoice",
        "fields": [{"name": "total", "type": "money_jpy",
                    "region": {"page": "last", "rect": [0.5, 0.5, 1.0, 0.75]}}],
    }
    out = llm_nodes._schema_for_prompt(schema, _PAGES_3)
    # p3 は 800x1600
    assert out["fields"][0]["region_px"] == {"page": 3, "bbox": [400, 800, 800, 1200]}


def test_存在しないページを指す領域はヒントごと落とす(monkeypatch) -> None:
    """1 ページ目へ縮退させると、まったく違う場所を指すヒントになり誤誘導になる。"""
    monkeypatch.setenv("REGION_KIE_HINTS", "1")
    schema = {
        "doc_type": "invoice",
        "fields": [{"name": "total", "type": "money_jpy",
                    "region": {"page": 9, "rect": [0.1, 0.1, 0.2, 0.2]}}],
    }
    out = llm_nodes._schema_for_prompt(schema, _PAGES_1)
    assert "region_px" not in out["fields"][0]
    assert "region" not in out["fields"][0]


def test_寸法の無いページの領域はヒントごと落とす(monkeypatch) -> None:
    monkeypatch.setenv("REGION_KIE_HINTS", "1")
    out = llm_nodes._schema_for_prompt(_SCHEMA_REGION, [{"page_no": 1, "image_uri": "x"}])
    assert "region_px" not in out["fields"][0]


def test_ヒントがあるときだけ_span_に座標を載せる(monkeypatch) -> None:
    """座標を常に載せると領域を使っていない run のプロンプトまで膨らむ。"""
    monkeypatch.setenv("REGION_KIE_HINTS", "1")
    provider = FakeProvider([_KIE_RESP])
    llm_nodes.make_kie_extract(LLMAdapter(provider), _BUNDLE)(
        {"spans": _SPANS, "layout_markdown": "", "schema": _SCHEMA_REGION, "pages": _PAGES_1}
    )
    user = provider.calls[0][1]
    assert '"bbox"' in user and '"region_px"' in user

    provider2 = FakeProvider([_KIE_RESP])
    llm_nodes.make_kie_extract(LLMAdapter(provider2), _BUNDLE)(
        {"spans": _SPANS, "layout_markdown": "",
         "schema": {"doc_type": "invoice", "fields": [{"name": "memo", "type": "string"}]},
         "pages": _PAGES_1}
    )
    assert '"bbox"' not in provider2.calls[0][1]


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
    out = llm_nodes._schema_for_prompt(schema, [{"page_no": 1, "width": 1000, "height": 2000}])
    assert "region_px" not in out["fields"][0]
    assert "region" not in out["fields"][0]
    assert out["fields"][0]["columns"] == [{"name": "item", "type": "string"}]
