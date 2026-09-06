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
