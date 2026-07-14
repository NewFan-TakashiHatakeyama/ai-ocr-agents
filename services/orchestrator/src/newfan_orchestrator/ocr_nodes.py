"""structure_ocr ノード（§5.3, DD-02）を paddle_client で実体化する。

各ページを structure-svc /layout-parsing で処理し、spans/layout/markdown を構築する。
DD-02 の単文字座標補完（低確信 span を crop して /ocr 再問合せ）は char_backfill フックで
差し込める（未指定なら主経路のみ）。build_graph に client/image_loader を渡すと有効化される。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import urlparse

from newfan_paddle_client import (
    LayoutParsingResponse,
    build_layout_blocks,
    build_spans,
    encode_image,
)
from newfan_schemas import ExtractionState, ReviewItem, SpanSource

NodeFn = Callable[[ExtractionState], dict[str, Any]]
ImageLoader = Callable[[str], bytes]


class StructureClient(Protocol):
    def layout_parsing(self, file_b64: str, *, file_type: int = 1) -> LayoutParsingResponse: ...


def file_uri_loader(uri: str) -> bytes:
    """file:// またはローカルパスの画像を読む（dev）。S3 は別ローダを注入する。"""
    parsed = urlparse(uri)
    if parsed.scheme in ("file", ""):
        path = Path(parsed.path if parsed.scheme == "file" else uri)
        return path.read_bytes()
    raise ValueError(f"未対応の image_uri スキーム: {uri}")


def make_structure_ocr(
    client: StructureClient,
    image_loader: ImageLoader = file_uri_loader,
) -> NodeFn:
    def _node(state: ExtractionState) -> dict[str, Any]:
        spans = []
        layout = []
        markdown_parts: list[str] = []
        errors: list[dict[str, Any]] = list(state.get("errors", []))
        next_span_id = 0

        for page in state.get("pages", []):
            page_no = int(page["page_no"])
            try:
                data = image_loader(str(page["image_uri"]))
                resp = client.layout_parsing(encode_image(data), file_type=1)
            except Exception as exc:  # noqa: BLE001 - ページ単位で errors に積み継続（§10）
                errors.append({"page": page_no, "stage": "structure_ocr", "error": str(exc)})
                continue

            if not resp.layout_parsing_results:
                errors.append({"page": page_no, "stage": "structure_ocr", "error": "empty result"})
                continue

            elem = resp.layout_parsing_results[0]
            page_spans = build_spans(elem.pruned_result, page=page_no, start_id=next_span_id)
            next_span_id += len(page_spans)
            spans.extend(page_spans)
            layout.extend(build_layout_blocks(elem.pruned_result, page=page_no))
            if elem.markdown is not None and elem.markdown.text:
                markdown_parts.append(elem.markdown.text)

        # TODO(DD-02): confidence<0.90 かつ char_boxes 欠落の span を crop→/ocr 再問合せで補完。
        return {
            "spans": spans,
            "layout": layout,
            "layout_markdown": "\n\n".join(markdown_parts),
            "errors": errors,
        }

    return _node


def make_vl_fallback(
    client: StructureClient,
    image_loader: ImageLoader = file_uri_loader,
) -> NodeFn:
    """vl_fallback ノード（§5.4, DD-09）を vl-svc で実体化する。

    品質ゲート NG ページ（fallback_pages）のみ VL に送る。得られた span は source='vl' で
    既存 OCR span と**併存**（破棄しない）。VL 由来は grounding 上限 0.7（confidence_score が
    SpanSource.VL を見て強制、DD-09）。VL 失敗ページは review_items 直行（未抽出ページ, §4.3）。
    """

    def _node(state: ExtractionState) -> dict[str, Any]:
        fallback_pages = sorted(set(state.get("fallback_pages", [])))
        if not fallback_pages:
            return {}

        pages_by_no = {int(p["page_no"]): p for p in state.get("pages", [])}
        spans = list(state.get("spans", []))
        layout = list(state.get("layout", []))
        review_items = list(state.get("review_items", []))
        errors: list[dict[str, Any]] = list(state.get("errors", []))
        next_span_id = max((s.span_id for s in spans), default=-1) + 1

        for page_no in fallback_pages:
            page = pages_by_no.get(page_no)
            if page is None:
                continue
            try:
                data = image_loader(str(page["image_uri"]))
                resp = client.layout_parsing(encode_image(data), file_type=1)
            except Exception as exc:  # noqa: BLE001 - 失敗ページはレビュー直行（§4.3）
                errors.append({"page": page_no, "stage": "vl_fallback", "error": str(exc)})
                review_items.append(
                    ReviewItem(field_name=f"page_{page_no}", reason="VLフォールバック失敗（未抽出ページ）", page=page_no)
                )
                continue

            if not resp.layout_parsing_results:
                review_items.append(
                    ReviewItem(field_name=f"page_{page_no}", reason="VL結果なし（未抽出ページ）", page=page_no)
                )
                continue

            elem = resp.layout_parsing_results[0]
            vl_spans = build_spans(
                elem.pruned_result, page=page_no, start_id=next_span_id, source=SpanSource.VL
            )
            next_span_id += len(vl_spans)
            spans.extend(vl_spans)  # 既存 OCR span を破棄せず併存
            layout.extend(build_layout_blocks(elem.pruned_result, page=page_no))

        return {"spans": spans, "layout": layout, "review_items": review_items, "errors": errors}

    return _node
