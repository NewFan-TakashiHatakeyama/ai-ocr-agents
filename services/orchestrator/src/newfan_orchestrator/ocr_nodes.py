"""structure_ocr ノード（§5.3, DD-02）を paddle_client で実体化する。

各ページを structure-svc /layout-parsing で処理し、spans/layout/markdown を構築する。
DD-02 の単文字座標補完（低確信 span を crop して /ocr 再問合せ）は char_backfill フックで
差し込める（未指定なら主経路のみ）。build_graph に client/image_loader を渡すと有効化される。
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Any, Callable, Optional, Protocol
from urllib.parse import urlparse
from urllib.request import url2pathname

from newfan_metrics import ocr_page_latency_seconds, ocr_pages_total
from newfan_paddle_client import (
    LayoutParsingResponse,
    OcrResponse,
    build_layout_blocks,
    build_spans,
    build_tables,
    encode_image,
)
from newfan_schemas import ExtractionState, ReviewItem, Span, SpanSource

from newfan_orchestrator.region_mask import (
    RegionStats,
    filter_spans,
    mask_tables,
    regions_for_page,
)

logger = logging.getLogger(__name__)

NodeFn = Callable[[ExtractionState], dict[str, Any]]
ImageLoader = Callable[[str], bytes]


class StructureClient(Protocol):
    def layout_parsing(self, file_b64: str, *, file_type: int = 1) -> LayoutParsingResponse: ...


class OcrClient(Protocol):
    def ocr(self, file_b64: str, *, file_type: int = 1) -> OcrResponse: ...


def _backfill(spans: list[Span], image_bytes: bytes, ocr_client: OcrClient, threshold: float) -> None:
    """DD-02: 低確信 span を bbox で crop→/ocr 再認識し text/conf を改善する。

    単語/単文字座標が返れば page 座標へ変換して char_boxes に格納（前方互換）。
    pillow 未導入や再問合せ失敗時は主経路の span を維持する（§10 継続）。
    """
    try:
        import io

        from PIL import Image  # runtime extra（pillow）
    except ImportError:
        return

    img: Any = None
    for s in spans:
        if s.conf >= threshold or not s.bbox:
            continue
        if img is None:
            img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        x1, y1, x2, y2 = s.bbox
        ox, oy = max(0, x1 - 2), max(0, y1 - 2)
        crop = img.crop((ox, oy, x2 + 2, y2 + 2))
        buf = io.BytesIO()
        crop.save(buf, format="PNG")
        try:
            resp = ocr_client.ocr(base64.b64encode(buf.getvalue()).decode("ascii"))
        except Exception:  # noqa: BLE001 - 再問合せ失敗は主経路維持
            continue
        if not resp.ocr_results:
            continue
        res = resp.ocr_results[0].pruned_result
        if not res.rec_texts:
            continue
        bi = max(range(len(res.rec_texts)), key=lambda i: res.rec_scores[i] if i < len(res.rec_scores) else 0.0)
        new_conf = float(res.rec_scores[bi]) if bi < len(res.rec_scores) else s.conf
        if new_conf > s.conf:  # より確信度の高い再読を採用
            s.text = res.rec_texts[bi]
            s.conf = new_conf
        wb = res.rec_word_boxes or res.text_word_boxes
        rects = [_word_box_rect(b) for b in wb or []]
        if rects and all(r is not None for r in rects):
            s.char_boxes = [[r[0] + ox, r[1] + oy, r[2] + ox, r[3] + oy] for r in rects]  # type: ignore[index]


def _word_box_rect(b: Any) -> Optional[list[int]]:
    """word box を矩形 [x1,y1,x2,y2] に正規化する。

    /ocr は矩形 [x1,y1,x2,y2] と 4 点ポリゴン [[x,y],...] の両形で返す。ポリゴン形は
    len(b)==4 の矩形ガードを素通りして int([x,y]) で TypeError になり、ジョブが
    ACK されず永久再配信になった（実 AWS の S3 トリガー E2E で検出）。crop 毎に
    形が変わり得るため、どちらでも受けて外接矩形へ落とす。判別できない形は None。
    """
    if not isinstance(b, (list, tuple)) or len(b) != 4:
        return None
    if all(isinstance(v, (int, float)) for v in b):
        return [int(b[0]), int(b[1]), int(b[2]), int(b[3])]
    if all(isinstance(p, (list, tuple)) and len(p) == 2
           and all(isinstance(v, (int, float)) for v in p) for p in b):
        xs = [p[0] for p in b]
        ys = [p[1] for p in b]
        return [int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))]
    return None


def file_uri_loader(uri: str) -> bytes:
    """file:// またはローカルパスの画像を読む（dev）。S3 は別ローダを注入する。

    file:// はプラットフォーム非依存に変換する（Windows の `file:///C:/...` を正しく扱う）。
    """
    parsed = urlparse(uri)
    if parsed.scheme == "file":
        return Path(url2pathname(parsed.path)).read_bytes()
    if parsed.scheme == "":
        return Path(uri).read_bytes()
    raise ValueError(f"未対応の image_uri スキーム: {uri}")


def _merge_region_metrics(
    state: ExtractionState, stats: RegionStats, *, active: bool
) -> dict[str, Any]:
    """``metrics["region"]`` を積算する。

    metrics は reducer 無しの LastValue チャネルなので、既存値を読んでから返さないと
    先に走ったノードの記録が消える（structure_ocr → vl_fallback の順で両方が書く）。

    除外領域が 1 つも設定されていない run では **region キー自体を作らない**
    （active=False）。全 run に 0 埋めの region を書くと「領域を使っていない run は
    完全に no-op」という後方互換の条件が崩れ、既存 run との metrics 差分が出る。
    領域が設定されている run は 0 件でも記録する（設定は効いているが何も消して
    いない、という情報自体に意味がある）。
    """
    metrics = dict(state.get("metrics", {}))
    if not active and "region" not in metrics:
        return metrics
    cur = dict(metrics.get("region", {}) or {})
    add = stats.as_dict()
    for key in ("excluded_spans", "excluded_cells", "excluded_rows"):
        cur[key] = int(cur.get(key, 0)) + int(add[key])
    for key in ("skipped_pages_no_dims", "markdown_dropped_pages"):
        cur[key] = sorted(set(list(cur.get(key, []) or []) + add[key]))
    metrics["region"] = cur
    return metrics


def make_structure_ocr(
    client: StructureClient,
    image_loader: ImageLoader = file_uri_loader,
    *,
    ocr_client: Optional[OcrClient] = None,
    backfill_threshold: float = 0.90,
) -> NodeFn:
    def _node(state: ExtractionState) -> dict[str, Any]:
        spans = []
        layout = []
        tables = []
        markdown_parts: list[str] = []
        errors: list[dict[str, Any]] = list(state.get("errors", []))
        next_span_id = 0

        exclude_regions = list(state.get("exclude_regions", []) or [])
        page_count = len(state.get("pages", []))
        rstats = RegionStats()

        tenant = state.get("tenant_id", "unknown")
        for page in state.get("pages", []):
            page_no = int(page["page_no"])
            try:
                data = image_loader(str(page["image_uri"]))
                # §12.1 ocr_page_latency_seconds{engine} / ocr_pages_total{tenant,engine}。
                # 失敗ページは数えない（成功したページの所要を測る指標のため）。
                with ocr_page_latency_seconds.labels(engine="structure").time():
                    resp = client.layout_parsing(encode_image(data), file_type=1)
                ocr_pages_total.labels(tenant=tenant, engine="structure").inc()
            except Exception as exc:  # noqa: BLE001 - ページ単位で errors に積み継続（§10）
                # errors に積むだけだと state の中に埋もれ、運用側からは「spans が 0 件で
                # LLM が幻覚を返す」という結果しか見えない（実 AWS で構造抽出が丸ごと
                # 飛んでいるのに気付けなかった）。継続方針は維持しつつ必ず記録する。
                logger.exception(
                    "[structure_ocr] ページ処理に失敗（継続）: page=%s image_uri=%s",
                    page_no,
                    page.get("image_uri"),
                )
                errors.append({"page": page_no, "stage": "structure_ocr", "error": str(exc)})
                continue

            if not resp.layout_parsing_results:
                logger.warning("[structure_ocr] 空の結果: page=%s", page_no)
                errors.append({"page": page_no, "stage": "structure_ocr", "error": "empty result"})
                continue

            elem = resp.layout_parsing_results[0]
            page_spans = build_spans(elem.pruned_result, page=page_no, start_id=next_span_id)
            # 除外領域は **_backfill より前**に適用する。後だと印影の OCR ゴミに
            # crop 再認識の課金が乗る。span_id の採番はフィルタ**前**の件数で
            # 進める（フィルタ後件数だと次ページの id と衝突する）。
            raw_span_count = len(page_spans)
            w, h = page.get("width"), page.get("height")
            px_regions = regions_for_page(exclude_regions, page_no, page_count, w, h)
            if exclude_regions and not px_regions and not (w and h):
                # 寸法不明のページは適用せず記録に残す（fail-open。§4.6）。
                # 無音で素通しすると「除外したのに出ている」の原因が追えない。
                logger.warning(
                    "[structure_ocr] ページ寸法が無いため除外領域を適用しません: page=%s", page_no
                )
                rstats.skipped_pages_no_dims.append(page_no)
            page_spans, n_excluded = filter_spans(page_spans, px_regions)
            rstats.excluded_spans += n_excluded
            # DD-02: 低確信 span を crop→/ocr 再認識で補完（ocr_client 注入時のみ）
            if ocr_client is not None:
                _backfill(page_spans, data, ocr_client, backfill_threshold)
            spans.extend(page_spans)
            layout.extend(build_layout_blocks(elem.pruned_result, page=page_no))
            # 構造由来テーブル（cell_box を span でグラウンディング, §5.3）
            page_tables, mask_stats = mask_tables(
                build_tables(elem.pruned_result, page_spans, page=page_no), px_regions
            )
            tables.extend(page_tables)
            rstats.excluded_cells += mask_stats.cells
            rstats.excluded_rows += mask_stats.rows
            next_span_id += raw_span_count
            if elem.markdown is not None and elem.markdown.text:
                if px_regions:
                    # markdown は座標を持たないため部分マスクが構造的に不可能で、
                    # ページ単位で落とすしかない。KIE の主要入力の 1 つを丸ごと
                    # 失うが、「除外領域の文字は DB に載せない」保証を優先する。
                    rstats.markdown_dropped_pages.append(page_no)
                else:
                    markdown_parts.append(elem.markdown.text)

        return {
            "spans": spans,
            "layout": layout,
            "tables": tables,
            "layout_markdown": "\n\n".join(markdown_parts),
            "errors": errors,
            "metrics": _merge_region_metrics(state, rstats, active=bool(exclude_regions)),
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
        exclude_regions = list(state.get("exclude_regions", []) or [])
        page_count = len(state.get("pages", []))
        rstats = RegionStats()
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
                with ocr_page_latency_seconds.labels(engine="vl").time():
                    resp = client.layout_parsing(encode_image(data), file_type=1)
                ocr_pages_total.labels(
                    tenant=state.get("tenant_id", "unknown"), engine="vl"
                ).inc()
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
            # 採番はフィルタ前件数で進める（既存行のまま）。除外は extend の直前で
            # 掛ける——順序を入れ替えると複数の fallback ページで span_id が衝突する。
            next_span_id += len(vl_spans)
            w, h = page.get("width"), page.get("height")
            px_regions = regions_for_page(exclude_regions, page_no, page_count, w, h)
            if exclude_regions and not px_regions and not (w and h):
                logger.warning(
                    "[vl_fallback] ページ寸法が無いため除外領域を適用しません: page=%s", page_no
                )
                rstats.skipped_pages_no_dims.append(page_no)
            vl_spans, n_excluded = filter_spans(vl_spans, px_regions)
            rstats.excluded_spans += n_excluded
            spans.extend(vl_spans)  # 既存 OCR span を破棄せず併存
            layout.extend(build_layout_blocks(elem.pruned_result, page=page_no))

        return {
            "spans": spans,
            "layout": layout,
            "review_items": review_items,
            "errors": errors,
            "metrics": _merge_region_metrics(state, rstats, active=bool(exclude_regions)),
        }

    return _node
