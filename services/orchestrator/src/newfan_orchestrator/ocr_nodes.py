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
from newfan_schemas import ExtractionState

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
