"""structure_ocr ノードの実体化（paddle_client の応答→spans/layout/markdown）。"""

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


def test_structure_ocr_page_error_is_collected() -> None:
    def bad_loader(uri: str) -> bytes:
        raise OSError("cannot read")

    node = ocr_nodes.make_structure_ocr(_FakeStructureClient(), bad_loader)
    out = node({"pages": [{"page_no": 3, "image_uri": "file:///x.png"}]})
    # ページ失敗は errors に積んで継続（§10）
    assert out["spans"] == []
    assert out["errors"][0]["page"] == 3


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
