"""RegionRect の読取領域ヒント 3 項目と予約名（設計 region-field-add-and-hint-v2 §2.3 / D8 / D9）。

固定するのは保存契約: 3 項目が往復すること、example_value が消毒されること
（LLM プロンプトに入る文字列を無検疫で通さない）、既存の領域データ（3 項目を
持たない JSON）が読めなくならないこと、予約名がサーバ側で拒まれること。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from newfan_schemas import (
    EXAMPLE_VALUE_MAX_LEN,
    LOST_PAGE_FIELD,
    REGION_AGGREGATE_FIELD,
    RESERVED_FIELD_NAME_PREFIX,
    REVIEW_AGGREGATE_FIELD_NAMES,
    FieldDef,
    FieldSchema,
    RegionRect,
    check_field_name,
)

RECT = [0.30, 0.02, 0.72, 0.09]


def _rect(**kw: object) -> RegionRect:
    return RegionRect.model_validate({"page": 1, "rect": RECT, **kw})


# ---------- 3 項目の往復 ----------


def test_hint_fields_roundtrip() -> None:
    src = {
        "page": 1,
        "rect": RECT,
        "example_value": "株式会社千曲川ホーム",
        "origin": "ghost",
        "created_at": "2026-09-11T09:30:00+09:00",
    }
    r = RegionRect.model_validate(src)
    assert r.example_value == "株式会社千曲川ホーム"
    assert r.origin == "ghost"
    # created_at は tz 付き UTC の 1 形式に正規化される（§2.3。naive/aware 混在を防ぐ）
    assert r.created_at == "2026-09-11T00:30:00Z"
    # dump → validate で値が変わらない（保存 → 取得の往復。正規化は冪等）
    again = RegionRect.model_validate(r.model_dump())
    assert again == r
    assert r.model_dump()["example_value"] == "株式会社千曲川ホーム"


def test_legacy_region_without_hint_fields_still_loads() -> None:
    """0007 世代の領域 JSON（3 項目のキー自体が無い）が読めなくならないこと。"""
    r = RegionRect.model_validate({"page": "last", "rect": RECT, "label": "合計"})
    assert r.example_value is None
    assert r.origin is None
    assert r.created_at is None
    # 全部 None のとき dump にもキーは載る（DTO 側で extra="ignore" に落とされない
    # ことは gateway の往復テストで別途固定する）
    assert set(r.model_dump()) >= {"example_value", "origin", "created_at"}


# ---------- example_value の消毒 ----------


def test_example_value_drops_control_characters() -> None:
    # U+0000 は Pg の TEXT に入らず save ごと落ちる。改行・タブも印字可能ではない
    r = _rect(example_value="株式会社\x00千曲川\nホーム\t")
    assert r.example_value == "株式会社千曲川ホーム"


def test_example_value_strips_and_truncates_at_limit() -> None:
    r = _rect(example_value="  " + "あ" * (EXAMPLE_VALUE_MAX_LEN + 1) + "  ")
    assert r.example_value is not None
    assert len(r.example_value) == EXAMPLE_VALUE_MAX_LEN == 200
    # ちょうど上限は切られない
    exact = _rect(example_value="い" * EXAMPLE_VALUE_MAX_LEN)
    assert exact.example_value == "い" * EXAMPLE_VALUE_MAX_LEN


@pytest.mark.parametrize("raw", ["", "   ", "\x00\x01", "\n\t"])
def test_example_value_empty_after_sanitize_becomes_none(raw: str) -> None:
    assert _rect(example_value=raw).example_value is None


def test_example_value_keeps_symbols_and_ideographic_space() -> None:
    """記号・全角文字・**全角スペース（U+3000）**は残る。

    ``str.isprintable()`` は Zs を印字不可とみなすが、日本語の社名・氏名の区切りとして
    紙面に普通に現れるので残す（設計 §2.3「正規化しない」）。kie.py の label の規則
    （isprintable のみ）とはここで分かれる。制御文字・書式文字は引き続き落ちる。
    """
    assert _rect(example_value="¥128,000　（税込）").example_value == "¥128,000　（税込）"
    assert _rect(example_value="株式会社　千曲川ホーム").example_value == "株式会社　千曲川ホーム"
    assert _rect(example_value="株式会社​千曲川").example_value == "株式会社千曲川"  # Cf は落ちる


# ---------- origin / created_at の値域 ----------


@pytest.mark.parametrize("origin", ["ghost", "manual", None])
def test_origin_accepts_known_values(origin: str | None) -> None:
    assert _rect(origin=origin).origin == origin


@pytest.mark.parametrize("origin", ["auto", "GHOST", "", "hand"])
def test_origin_rejects_unknown_values(origin: str) -> None:
    with pytest.raises(ValidationError):
        _rect(origin=origin)


@pytest.mark.parametrize(
    ("ts", "stored"),
    [
        ("2026-09-11T00:00:00Z", "2026-09-11T00:00:00Z"),  # JS の toISOString()
        ("2026-09-11T00:00:00.123Z", "2026-09-11T00:00:00.123000Z"),
        ("2026-09-11T09:00:00+09:00", "2026-09-11T00:00:00Z"),  # UTC へ
        ("2026-09-11T09:00:00", "2026-09-11T09:00:00Z"),  # naive は UTC とみなす
        ("2026-09-11", "2026-09-11T00:00:00Z"),
    ],
)
def test_created_at_normalizes_to_utc(ts: str, stored: str) -> None:
    """tz 付き UTC の 1 形式に正規化する。混在させると「有効化前の領域」の比較で
    naive と aware の TypeError になる。正規化は冪等（往復で揺れない）。"""
    assert _rect(created_at=ts).created_at == stored
    assert _rect(created_at=stored).created_at == stored


@pytest.mark.parametrize("ts", ["yesterday", "", "Z", "2026/09/11", "1757548800"])
def test_created_at_rejects_non_iso8601(ts: str) -> None:
    with pytest.raises(ValidationError):
        _rect(created_at=ts)


# ---------- 除外領域に付いても無害 ----------


def test_exclude_region_with_hint_fields_is_harmless() -> None:
    """除外領域（page:null が正当）に 3 項目が付いていても検証は通る。

    3 項目は読取領域でしか使わないが、型を分けると DTO・DB・web で二重管理になる。
    「付いていても無視される」を契約にし、検証で落とさない。
    """
    r = RegionRect.model_validate(
        {
            "page": None,
            "rect": RECT,
            "label": "社印",
            "example_value": "印",
            "origin": "manual",
            "created_at": "2026-09-11T00:00:00Z",
        }
    )
    assert r.page is None and r.label == "社印"
    assert r.example_value == "印"


# ---------- 予約名（D9） ----------


def test_review_aggregate_names_are_the_gate_constants() -> None:
    """orchestrator が積む集約 ReviewItem の擬似 field 名と同じ定数であること。"""
    assert LOST_PAGE_FIELD == "__pages__"
    assert REGION_AGGREGATE_FIELD == "__region__"
    assert REVIEW_AGGREGATE_FIELD_NAMES == frozenset({"__pages__", "__region__"})
    assert RESERVED_FIELD_NAME_PREFIX == "__"


@pytest.mark.parametrize("name", ["__pages__", "__region__", "__anything"])
def test_check_field_name_rejects_reserved(name: str) -> None:
    with pytest.raises(ValueError, match="予約"):
        check_field_name(name)


@pytest.mark.parametrize("name", ["total_amount", "_private", "x__", "a__b", "_"])
def test_check_field_name_allows_ordinary_names(name: str) -> None:
    """先頭 ``__`` だけが予約。末尾や途中の ``__``・単独の ``_`` は通す（過剰拒否しない）。"""
    check_field_name(name)


@pytest.mark.parametrize("name", ["__pages__", "__region__", "__x"])
def test_field_def_reads_reserved_name_from_stored_data(name: str) -> None:
    """読み出しモデルは予約名を**拒まない**。

    検査導入前にチャット経路で保存された ``__`` 始まりの行が 1 つあるだけで、
    orchestrator の load_context（FieldSchema.model_validate）が落ちて run が止まる。
    拒否は書き込み側（gateway の put_schema）に限る。
    """
    assert FieldDef(name=name).name == name
    schema = FieldSchema.model_validate({"doc_type": "invoice", "fields": [{"name": name}]})
    assert [f.name for f in schema.fields] == [name]
