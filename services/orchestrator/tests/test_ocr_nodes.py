"""structure_ocr ノードの実体化（paddle_client の応答→spans/layout/markdown）。"""

import logging

import pytest
from newfan_paddle_client import LayoutParsingResponse

from newfan_orchestrator import ocr_nodes

# 2 ページ分の layout-parsing 応答（1 span/ページ）を返す fake client
_PAGE_RESULT = {
    "prunedResult": {
        "parsing_res_list": [
            {"block_bbox": [40, 40, 520, 90], "block_label": "text", "block_content": "本文", "block_id": 0, "block_order": 0}
        ],
        "overall_ocr_res": {
            "rec_texts": ["¥128,000"],
            "rec_scores": [0.72],
            "rec_polys": [[[300, 180], [430, 180], [430, 212], [300, 212]]],
        },
    },
    "markdown": {"text": "# 請求書", "isStart": True, "isEnd": True},
}


class _FakeStructureClient:
    def __init__(self) -> None:
        self.calls = 0

    def layout_parsing(self, file_b64: str, *, file_type: int = 1) -> LayoutParsingResponse:
        self.calls += 1
        return LayoutParsingResponse.model_validate({"layoutParsingResults": [_PAGE_RESULT]})


_TABLE_PAGE_RESULT = {
    "prunedResult": {
        "parsing_res_list": [],
        "overall_ocr_res": {
            "rec_texts": ["印"],
            "rec_scores": [0.9],
            "rec_polys": [[[600, 100], [900, 100], [900, 140], [600, 140]]],
        },
        "table_res_list": [
            {
                "pred_html": "<table><tr><td>品名</td><td>備考</td></tr>"
                "<tr><td>りんご</td><td>印</td></tr></table>",
                "cell_box_list": [
                    [100, 40, 200, 80], [600, 40, 900, 80],
                    [100, 100, 200, 140], [600, 100, 900, 140],
                ],
            }
        ],
    },
    "markdown": {"text": "", "isStart": True, "isEnd": True},
}


class _FakeTableClient:
    def layout_parsing(self, file_b64: str, *, file_type: int = 1) -> LayoutParsingResponse:
        return LayoutParsingResponse.model_validate(
            {"layoutParsingResults": [_TABLE_PAGE_RESULT]}
        )


def _loader(uri: str) -> bytes:
    return b"\x89PNG-fake"


def test_structure_ocr_builds_spans_across_pages() -> None:
    client = _FakeStructureClient()
    node = ocr_nodes.make_structure_ocr(client, _loader)
    state = {
        "pages": [
            {"page_no": 1, "image_uri": "file:///p1.png"},
            {"page_no": 2, "image_uri": "file:///p2.png"},
        ]
    }
    out = node(state)

    assert client.calls == 2
    # 跨ページで span_id が連番化
    assert [s.span_id for s in out["spans"]] == [0, 1]
    assert [s.page for s in out["spans"]] == [1, 2]
    assert out["spans"][0].bbox == [300, 180, 430, 212]  # rec_polys→bbox
    assert out["layout"][0].label == "text"
    assert "# 請求書" in out["layout_markdown"]


def test_structure_ocr_page_error_is_collected(caplog: pytest.LogCaptureFixture) -> None:
    def bad_loader(uri: str) -> bytes:
        raise OSError("cannot read")

    node = ocr_nodes.make_structure_ocr(_FakeStructureClient(), bad_loader)
    with caplog.at_level(logging.ERROR):
        out = node({"pages": [{"page_no": 3, "image_uri": "file:///x.png"}]})
    # ページ失敗は errors に積んで継続（§10）
    assert out["spans"] == []
    assert out["errors"][0]["page"] == 3
    # errors に積むだけだと state に埋もれ、運用側からは「spans 0 件で LLM が幻覚を返す」
    # という結果しか見えない。必ずスタックトレースを残すこと（実 AWS で踏んだ）。
    assert "structure_ocr" in caplog.text
    assert "file:///x.png" in caplog.text
    assert "cannot read" in caplog.text


def test_vl_fallback_merges_vl_spans() -> None:
    from newfan_schemas import SpanSource

    existing = build_spans_state()
    node = ocr_nodes.make_vl_fallback(_FakeStructureClient(), _loader)
    out = node(
        {
            "fallback_pages": [2],
            "pages": [{"page_no": 2, "image_uri": "file:///p2.png"}],
            "spans": existing,
        }
    )
    # 既存 OCR span は併存、VL span が source='vl' で追加（span_id は連番継続）
    assert len(out["spans"]) == len(existing) + 1
    vl_span = out["spans"][-1]
    assert vl_span.source is SpanSource.VL
    assert vl_span.span_id == existing[-1].span_id + 1


def test_vl_fallback_failure_routes_to_review() -> None:
    def bad_loader(uri: str) -> bytes:
        raise OSError("vl unreachable")

    node = ocr_nodes.make_vl_fallback(_FakeStructureClient(), bad_loader)
    out = node(
        {"fallback_pages": [5], "pages": [{"page_no": 5, "image_uri": "file:///p5.png"}]}
    )
    assert out["review_items"][0].page == 5
    assert "未抽出" in out["review_items"][0].reason


def test_vl_fallback_noop_without_fallback_pages() -> None:
    node = ocr_nodes.make_vl_fallback(_FakeStructureClient(), _loader)
    assert node({"fallback_pages": []}) == {}


def build_spans_state() -> list:
    from newfan_schemas import Span

    return [Span(span_id=0, page=1, text="既存", conf=0.9, bbox=[0, 0, 1, 1])]


# ---------- 除外領域（設計 §5.2） ----------

# 右上 1/4 を覆う領域。_PAGE_RESULT の span bbox は [300,180,430,212] なので、
# ページを 1000x1000 とすると領域 [500,0,1000,500] には入らない（残る）。
_STAMP_RIGHT = {"page": None, "rect": [0.5, 0.0, 1.0, 0.5]}
# span を全没させる領域
_COVER_SPAN = {"page": None, "rect": [0.2, 0.1, 0.6, 0.3]}
_DIMS = {"width": 1000, "height": 1000}


def _pages(n: int = 2, **extra: object) -> list[dict]:
    return [
        {"page_no": i, "image_uri": f"file:///p{i}.png", **extra} for i in range(1, n + 1)
    ]


def test_structure_ocr_filters_spans_before_backfill() -> None:
    """除外は再 OCR（DD-02 backfill）より前に掛ける。

    後だと印影の OCR ゴミ文字に crop 再認識の課金が乗る。backfill が呼ばれた
    span の件数で順序を観測する。
    """
    seen: list[str] = []

    class _Ocr:
        def ocr(self, file_b64: str, **kw: object):  # pragma: no cover - 呼ばれない想定
            seen.append(file_b64)
            raise AssertionError("除外済み span に再 OCR が走った")

    node = ocr_nodes.make_structure_ocr(
        _FakeStructureClient(), _loader, ocr_client=_Ocr(), backfill_threshold=0.99
    )
    out = node({"pages": _pages(1, **_DIMS), "exclude_regions": [_COVER_SPAN]})
    assert out["spans"] == []
    assert seen == []
    assert out["metrics"]["region"]["excluded_spans"] == 1


def test_span_id_no_collision_across_pages_with_filter() -> None:
    """span_id の採番はフィルタ**前**の件数で進める。

    フィルタ後の件数で進めると、1 ページ目で除外が起きた瞬間に 2 ページ目の
    span_id が 1 ページ目と衝突し、KIE の根拠参照が別ページの文字を指す。
    """
    node = ocr_nodes.make_structure_ocr(_FakeStructureClient(), _loader)
    pages = _pages(2, **_DIMS)
    # 1 ページ目だけ span を全没させる
    out = node({"pages": pages, "exclude_regions": [{"page": 1, "rect": [0.2, 0.1, 0.6, 0.3]}]})
    assert [(s.span_id, s.page) for s in out["spans"]] == [(1, 2)]
    assert out["metrics"]["region"]["excluded_spans"] == 1


def test_exclude_noop_without_regions() -> None:
    """除外領域が無ければ現行と完全に同じ（テンプレートレス運用に退行なし）。"""
    node = ocr_nodes.make_structure_ocr(_FakeStructureClient(), _loader)
    base = node({"pages": _pages(2)})
    with_key = node({"pages": _pages(2), "exclude_regions": []})
    assert [s.model_dump() for s in base["spans"]] == [s.model_dump() for s in with_key["spans"]]
    assert base["layout_markdown"] == with_key["layout_markdown"] != ""
    # 領域を使っていない run には region キー自体を作らない（metrics も現行と同じ）
    assert "region" not in base["metrics"]
    assert "region" not in with_key["metrics"]


def test_exclude_noop_when_page_dims_missing() -> None:
    """寸法の無いページでは除外を適用せず、記録だけ残して完走する（C26 / C34）。

    pages.width/height は DDL で nullable で、既存テストの seed も寸法を持たない。
    ここで KeyError や None 演算を投げると、ocr_nodes の try/except は
    build_spans 以降を覆っていないためノードごと落ち、worker が ACK しないまま
    60 秒ごとに再配信され続ける。
    """
    node = ocr_nodes.make_structure_ocr(_FakeStructureClient(), _loader)
    out = node({"pages": _pages(2), "exclude_regions": [_COVER_SPAN]})
    assert len(out["spans"]) == 2  # 除外されていない
    assert out["metrics"]["region"]["skipped_pages_no_dims"] == [1, 2]
    assert out["metrics"]["region"]["excluded_spans"] == 0


def test_markdown_skipped_on_excluded_page() -> None:
    """除外領域を持つページの markdown は丸ごと落とす。

    markdown は座標を持たず部分マスクが構造的に不可能なので、「除外領域の文字を
    DB に載せない」保証を精度より優先する。落としたことは metrics に残す。
    """
    node = ocr_nodes.make_structure_ocr(_FakeStructureClient(), _loader)
    out = node({"pages": _pages(2, **_DIMS), "exclude_regions": [{"page": 1, "rect": [0.5, 0.0, 1.0, 0.5]}]})
    # 2 ページ分あった markdown が 1 ページ分だけになる
    assert out["layout_markdown"].count("# 請求書") == 1
    assert out["metrics"]["region"]["markdown_dropped_pages"] == [1]


def test_structure_ocr_masks_table_cells() -> None:
    node = ocr_nodes.make_structure_ocr(_FakeTableClient(), _loader)
    out = node({"pages": _pages(1, **_DIMS), "exclude_regions": [_STAMP_RIGHT]})
    assert out["metrics"]["region"]["excluded_cells"] >= 1
    assert out["tables"][0].structure_html is None


def test_vl_fallback_filters_after_id_advance() -> None:
    """VL 側も除外する。span_id の加算はフィルタ前（複数ページで衝突しない）。"""
    node = ocr_nodes.make_vl_fallback(_FakeStructureClient(), _loader)
    state = {
        "pages": _pages(2, **_DIMS),
        "fallback_pages": [1, 2],
        "spans": [],
        # 1 ページ目の span だけ全没させる
        "exclude_regions": [{"page": 1, "rect": [0.2, 0.1, 0.6, 0.3]}],
    }
    out = node(state)
    assert [(s.span_id, s.page) for s in out["spans"]] == [(1, 2)]
    assert out["metrics"]["region"]["excluded_spans"] == 1


def test_region_metrics_accumulate_across_nodes() -> None:
    """structure_ocr の記録を vl_fallback が上書きしない。

    metrics は reducer 無しの LastValue チャネルなので、既存値を読まずに返すと
    先に走ったノードの件数が消える。
    """
    structure = ocr_nodes.make_structure_ocr(_FakeStructureClient(), _loader)
    state = {"pages": _pages(2, **_DIMS), "exclude_regions": [_COVER_SPAN]}
    after_ocr = structure(state)
    assert after_ocr["metrics"]["region"]["excluded_spans"] == 2

    vl = ocr_nodes.make_vl_fallback(_FakeStructureClient(), _loader)
    out = vl({**state, **after_ocr, "fallback_pages": [1]})
    assert out["metrics"]["region"]["excluded_spans"] == 3  # 2 + 1（上書きされない）
