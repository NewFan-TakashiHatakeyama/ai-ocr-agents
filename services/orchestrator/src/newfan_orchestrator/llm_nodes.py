"""LLM 接続ノード（§4.3 kie_extract / llm_correct）を llm-adapter で実体化する。

build_graph に adapter/bundle を渡すと、スタブの代わりにこれらのノードが使われる。
DD-10 の適用制約は llm-adapter の llm_correct 側で強制済み。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from newfan_llm_adapter import LLMAdapter, PromptBundle, kie_extract, llm_correct
from newfan_metrics import correction_reuse_hits_total, current_tenant
from newfan_schemas import (
    ExtractedField,
    ExtractionState,
    FieldSchema,
    ReviewStatus,
    Span,
    sanitize_example_value,
)

from newfan_orchestrator.confidence import apply_correction_confidence
from newfan_orchestrator.region_hint import (
    REASON_PAGE_OUT_OF_RANGE,
    REASON_PAGE_UNPROJECTABLE,
    example_present,
    prevalidate,
    select_candidates,
    span_inside,
)
from newfan_orchestrator.region_mask import regions_for_page

NodeFn = Callable[[ExtractionState], dict[str, Any]]

logger = logging.getLogger(__name__)


def _rule_hints(active_rules: list[dict[str, Any]]) -> str:
    hints = [r.get("rule_json", {}).get("hint_text", "") for r in active_rules]
    return "\n".join(h for h in hints if h)


#: 読取領域ヒントを既定 on にした時点（設計 v2 §1.5 / §2.8）。これより前に
#: 引かれた領域（``created_at`` が無い・これより古い）は、作者がヒントとしての効果を
#: 確認していないので、検証画面で「有効化前に引かれた領域」として区別して見せる。
#: 環境ごとに実際に on にした時点が違うときは、環境変数 ``REGION_HINTS_ACTIVATED_AT``
#: で上書きできる（``region_hints_activated_at``）。
REGION_HINTS_ACTIVATED_AT = "2026-09-12T00:00:00Z"
_REGION_HINTS_ACTIVATED_DT = datetime.fromisoformat(
    REGION_HINTS_ACTIVATED_AT.replace("Z", "+00:00")
)

# ``REGION_HINTS_ACTIVATED_AT`` の値ごとの解釈結果。不正な値の warning を値ごとに 1 回に
# 抑えるため（run ごと・項目ごとに読むので、毎回出すとログが埋まる）。値はプロセスの
# 起動時に決まるので実質 1 件。テストは env を monkeypatch し、warning の回数を見るときは
# この辞書を空にしてから呼ぶ
_activated_at_cache: dict[str, Optional[datetime]] = {}


def _parse_iso_utc(raw: str) -> Optional[datetime]:
    """ISO 8601 の文字列を tz 付き datetime に読む。解釈できなければ None。

    末尾 ``Z``・オフセット付きのどちらも受け、tz の無い値（日付だけ等）は UTC とみなす。
    ``created_at``（RegionRect が UTC の Z 付きに正規化して保存する）と環境変数
    ``REGION_HINTS_ACTIVATED_AT`` の両方をこの 1 つの規則で読む。
    """
    probe = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
    try:
        dt = datetime.fromisoformat(probe)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def region_hints_activated_at() -> datetime:
    """「有効化前に引かれた領域」の境界となる時点（設計 v2 §1.5 / §2.8）。

    既定は定数 ``REGION_HINTS_ACTIVATED_AT``（コードで既定 on にした時点）。ただし
    実際に on になった時点は環境ごとにずれ得る（キルスイッチで止めていた期間があった、
    デプロイした日が違う等）。そのときは環境変数 ``REGION_HINTS_ACTIVATED_AT`` に
    実際に on にした時点（ISO 8601。末尾 ``Z`` かオフセット付き）を渡すと、注記の境界が
    それに合う。tz の無い値（日付だけ ``2026-10-01`` 等）は **UTC** とみなす（JST の 0 時
    ではない。JST の時点は ``2026-10-01T00:00:00+09:00`` のように書く。deploy 側の注記と
    terraform の validation はこの規則を前提にしている）。

    - 未設定・空文字は定数（compose の ``${REGION_HINTS_ACTIVATED_AT:-}`` が渡す値）
    - 解釈できない値は**定数に倒す**。黙って倒さず warning を出す（値ごとに 1 回）。
      境界を勝手に動かすより、既定の境界で注記が出続ける方が安全側
    - env は呼ぶたびに読む（テストが monkeypatch できる）。解釈の結果だけを値ごとに持つ
    """
    raw = os.environ.get("REGION_HINTS_ACTIVATED_AT", "").strip()
    if not raw:
        return _REGION_HINTS_ACTIVATED_DT
    if raw not in _activated_at_cache:
        parsed = _parse_iso_utc(raw)
        if parsed is None:
            logger.warning(
                "REGION_HINTS_ACTIVATED_AT=%r は ISO 8601 として解釈できないため無視し、"
                "既定の %s を有効化の時点として使う",
                raw,
                REGION_HINTS_ACTIVATED_AT,
            )
        _activated_at_cache[raw] = parsed
    parsed = _activated_at_cache[raw]
    return parsed if parsed is not None else _REGION_HINTS_ACTIVATED_DT


def region_hints_enabled() -> bool:
    """読取領域を KIE プロンプトのヒントとして渡すか（設計 v2 §2.10・**既定 on**）。

    出荷ゲート（§3 G1〜G6）を第 3 回計測（2026-09-12）で通したので既定 on にした。
    環境変数 ``REGION_KIE_HINTS`` は**キルスイッチ**である: 未設定・空文字は on、
    ``0`` / ``false`` / ``no`` / ``off``（大小文字・前後空白を無視）だけが off。
    それ以外の値（``1`` / ``true`` 等）は on。

    以前の実装は ``("1","true","yes","on")`` に入るときだけ on だったため、既定を
    コードで反転するだけでは explicit off が効かなかった（R19）。ここで意味を反転する。
    """
    value = os.environ.get("REGION_KIE_HINTS", "").strip().lower()
    return value not in ("0", "false", "no", "off")


def _hint_page_no(region: dict[str, Any], page_count: int) -> Optional[int]:
    """読取領域の page 指定を run のページ番号へ解決する。範囲外・不正は None。"""
    if page_count == 0:
        return None
    spec = region.get("page")
    if spec == "last":
        return page_count
    if isinstance(spec, int) and not isinstance(spec, bool):
        return spec if 1 <= spec <= page_count else None
    return None  # include は page 必須（None は除外領域のみ）


def _region_px(region: dict[str, Any], pages: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """正規化 rect を当該ページの画素矩形へ射影する（設計 §5.6-1）。

    - ``"last"`` は run の総ページ数へ解決する
    - **存在しないページを指す region はヒントごと落とす**。1 ページ目へ縮退させると
      まったく違う場所を指すヒントになり、誤誘導になる
    - 寸法が無いページも落とす（射影できない）
    """
    page_no = _hint_page_no(region, len(pages))
    if page_no is None:
        return None
    dim = next((p for p in pages if int(p.get("page_no", 0)) == page_no), None)
    if dim is None:
        return None
    w, h = dim.get("width"), dim.get("height")
    if not w or not h or w <= 0 or h <= 0:
        return None
    rect = region.get("rect")
    if not rect or len(rect) < 4:
        return None
    return {
        "page": page_no,
        "bbox": [
            int(round(float(rect[0]) * w)),
            int(round(float(rect[1]) * h)),
            int(round(float(rect[2]) * w)),
            int(round(float(rect[3]) * h)),
        ],
    }


def _is_pre_activation(created_at: Any) -> bool:
    """領域が有効化の時点（``region_hints_activated_at``）より前に引かれたものか（設計 v2 §1.5）。

    ``created_at`` は RegionRect が tz 付き UTC の ISO 8601（末尾 Z）に正規化して保存
    するが、JSONB は検査導入前のデータや手修正で規則を素通りし得る。**無い・解釈できない
    ものは「有効化前」に倒す**（列を足したのが有効化と同じ設計なので、無い＝古い。
    解釈できないものを「有効化後」と見なすと注記が消えて、作者が確認していない領域が
    黙って使われる側に倒れる）。
    """
    if not isinstance(created_at, str) or not created_at.strip():
        return True
    dt = _parse_iso_utc(created_at.strip())
    if dt is None:
        return True
    return dt < region_hints_activated_at()


@dataclass
class HintReport:
    """``metrics.region.hints`` に載せる観測値（設計 §2.5）。

    - given: ヒントを渡した項目名
    - dropped: 落とした項目名 → 理由（region_hint の理由定数）
    - truncated: 候補を HINT_MAX_CANDIDATES で切った項目名 → 切った件数
    - outcomes: 従った／捨てた（D15。KieResult.hint_outcomes から写す）
    - detail: 項目名 → 例示値と候補の原文（先頭数件）。検証画面の参考表示（§2.8）が
      「種類が合わない（例示: 株式会社〜 / 候補: 大熊邸）」と**何と何が合わなかったか**を
      出すため。理由の定数だけでは、テンプレートの作者が領域を引き直す判断ができない
    - pre_activation: 評価した（given か dropped に載った）項目のうち、領域の
      ``created_at`` が無い・有効化の時点（``region_hints_activated_at``。既定は
      ``REGION_HINTS_ACTIVATED_AT``）より前のもの（§1.5 / §2.8）。
      検証画面の**参考表示だけ**に使う。レビュー件数・確信度には触らない
    """

    given: list[str] = dc_field(default_factory=list)
    dropped: dict[str, str] = dc_field(default_factory=dict)
    truncated: dict[str, int] = dc_field(default_factory=dict)
    outcomes: dict[str, str] = dc_field(default_factory=dict)
    detail: dict[str, dict[str, Any]] = dc_field(default_factory=dict)
    pre_activation: list[str] = dc_field(default_factory=list)

    def __bool__(self) -> bool:
        """領域を持つ項目が 1 つでも評価されたか（given か dropped に載る）。"""
        return bool(self.given or self.dropped)

    def as_dict(self) -> dict[str, Any]:
        return {
            "given": list(self.given),
            "dropped": dict(self.dropped),
            "truncated": dict(self.truncated),
            "outcomes": dict(self.outcomes),
            "detail": {k: dict(v) for k, v in self.detail.items()},
            "pre_activation": list(self.pre_activation),
        }


# 参考表示に載せる原文の上限。metrics は run ごとに JSONB へ入るので、候補 12 件 × 200 字を
# そのまま写さない（表示に要るのは「何と何が合わなかったか」が分かる程度）
DETAIL_MAX_CANDIDATES = 3
DETAIL_MAX_CHARS = 40


def _clip(text: str) -> str:
    return text if len(text) <= DETAIL_MAX_CHARS else text[: DETAIL_MAX_CHARS - 1] + "…"


def _hint_detail(example_value: Optional[str], cands: list[Span]) -> dict[str, Any]:
    """候補は読み順（``select_candidates`` の戻り）で先頭数件。プロンプトと同じ並びで見せる。"""
    return {
        "example_value": _clip(example_value) if example_value else None,
        "candidates": [_clip(s.text) for s in cands[:DETAIL_MAX_CANDIDATES]],
    }


def build_region_hints(
    schema: dict[str, Any],
    pages: Optional[list[dict[str, Any]]] = None,
    spans: Optional[list[Span]] = None,
    exclude_px_by_page: Optional[dict[int, list[list[int]]]] = None,
) -> tuple[dict[str, Any], HintReport]:
    """kie プロンプトへ渡す schema と、ヒントの観測値を作る（設計 §5.6 / §4.7 / v2 §2.5）。

    kie.py は schema をそのまま ``json.dumps`` してプロンプトへ埋める。保存されている
    ``region`` は**正規化座標**なので、素通しすると LLM に「0.30」等の意味不明な数値が
    渡り、しかも領域を使っていないスキーマでも gateway が ``"region": null`` を書く
    だけでプロンプトが変わる（＝抽出結果が変わり得る）。よって ``region`` キーは
    **必ず落とす**。

    ヒントが有効なとき（既定 on。``REGION_KIE_HINTS`` はキルスイッチ）に限り、代わりに
    ``region_hint`` を載せる: 矩形の中にある span の候補列（id と原文）・例示値・
    例示値が候補にあるか（D13）。渡す前に決定論の事前ガード（``region_hint.prevalidate``）
    で落とし、落とした理由・切った候補数は report に残す（D14）。評価した項目のうち
    領域が有効化前に引かれたもの（``created_at`` が無い・古い）は ``pre_activation`` に
    残す（§1.5）。``spans`` を省略した呼び出し（旧来の呼び方）ではヒントを付けない。

    **領域を持たない field には何も足さない**ので、領域を使っていないスキーマの
    プロンプトはヒント有効化後も現行と 1 バイトも変わらない。

    state の schema は**変更しない**（LangGraph の state は他ノードと共有され、
    checkpoint にも載る。破壊的に書き換えると再開時の入力が変わる）。
    """
    report = HintReport()
    fields = schema.get("fields")
    if not isinstance(fields, list):
        return dict(schema), report
    hint = region_hints_enabled() and bool(pages) and spans is not None
    page_list = list(pages or [])
    span_list = list(spans or [])
    exclude_by_page = exclude_px_by_page or {}
    out = dict(schema)
    new_fields = []
    for f in fields:
        if not isinstance(f, dict) or "region" not in f:
            new_fields.append(f)
            continue
        stripped = {k: v for k, v in f.items() if k != "region"}
        region = f.get("region")
        # 明細（表）にはヒントを入れない。行数が増えたり次ページへ続いたりした
        # 帳票で「領域に近い行だけ」を選ばせると、行が静かに切り捨てられる。
        # 位置ガードも TableResult を見ないので、この壊れ方はどこにも掛からない。
        # name の無い field は metrics に記録しない（"" キーで上書きし合うだけで意味が無い）
        if hint and isinstance(region, dict) and not f.get("columns") and f.get("name"):
            name = str(f["name"])
            # 評価した項目（この先 given か dropped のどちらかに必ず載る）のうち、
            # 有効化前に引かれた領域を先に控える。参考表示（§2.8）にしか使わない
            if _is_pre_activation(region.get("created_at")):
                report.pre_activation.append(name)
            px = _region_px(region, page_list)
            if px is None:
                report.dropped[name] = (
                    REASON_PAGE_OUT_OF_RANGE
                    if _hint_page_no(region, len(page_list)) is None
                    else REASON_PAGE_UNPROJECTABLE
                )
                new_fields.append(stripped)
                continue
            page_no, rect = int(px["page"]), list(px["bbox"])
            inside = [s for s in span_list if s.page == page_no and span_inside(s.bbox, rect)]
            # 重なり面積で最大 HINT_MAX_CANDIDATES 件を選び、読み順で渡す（設計 v2 §2.5）
            cands, truncated = select_candidates(inside, rect)
            if truncated:
                report.truncated[name] = truncated
            # 例示値は LLM プロンプトに入る。保存時と同じ規則で渡す直前にも消毒する
            # （JSONB は検査導入前のデータや手修正で規則を素通りし得る）
            ev_raw = region.get("example_value")
            ev = sanitize_example_value(ev_raw) if isinstance(ev_raw, str) else None
            if ev or cands:
                report.detail[name] = _hint_detail(ev, cands)
            # 事前ガードは**切った後の候補**に当てる。切り捨て件数を残すのは、
            # 「13 件目に数字があったのに type_mismatch で落ちた」を後から追えるようにするため
            reason = prevalidate(f, cands, ev, rect, list(exclude_by_page.get(page_no, [])))
            if reason is not None:
                report.dropped[name] = reason
                new_fields.append(stripped)
                continue
            stripped["region_hint"] = {
                "candidates": [{"span_id": s.span_id, "text": s.text} for s in cands],
                "example_value": ev,
                "example_present": example_present(ev, cands),
            }
            report.given.append(name)
        new_fields.append(stripped)
    out["fields"] = new_fields
    return out, report


def _schema_for_prompt(
    schema: dict[str, Any],
    pages: Optional[list[dict[str, Any]]] = None,
    spans: Optional[list[Span]] = None,
    exclude_px_by_page: Optional[dict[int, list[list[int]]]] = None,
) -> dict[str, Any]:
    """``build_region_hints`` の schema だけを返す薄い別名（既存の呼び出し互換）。"""
    return build_region_hints(schema, pages, spans, exclude_px_by_page)[0]


def _exclude_px_by_page(state: ExtractionState) -> dict[int, list[list[int]]]:
    """適用済み除外領域の画素矩形をページごとに引き直す（``overlaps_exclude`` の判定用）。

    除外の適用自体は ocr_nodes が済ませている。ここでは同じ規則（``regions_for_page``）で
    再計算するだけで、span は触らない。
    """
    exclude_regions = list(state.get("exclude_regions", []) or [])
    pages = list(state.get("pages", []) or [])
    if not exclude_regions or not pages:
        return {}
    out: dict[int, list[list[int]]] = {}
    for p in pages:
        try:
            page_no = int(p["page_no"])
        except (KeyError, TypeError, ValueError):
            continue
        out[page_no] = regions_for_page(
            exclude_regions, page_no, len(pages), p.get("width"), p.get("height")
        )
    return out


def _merge_hint_metrics(state: ExtractionState, report: HintReport) -> dict[str, Any]:
    """``metrics.region.hints`` を書く（ocr_nodes._merge_region_metrics と同じ規則）。

    metrics は reducer 無しの LastValue チャネルなので、既存の ``region``
    （excluded_* / mismatch_fields 等）を読んでから返す。``hints`` は run ごとに
    **丸ごと書き直す**（再配信で前回の値を残さない）。
    """
    metrics = dict(state.get("metrics", {}) or {})
    region = dict(metrics.get("region", {}) or {})
    region["hints"] = report.as_dict()
    metrics["region"] = region
    return metrics


def make_kie_extract(adapter: LLMAdapter, bundle: PromptBundle) -> NodeFn:
    def _node(state: ExtractionState) -> dict[str, Any]:
        spans = list(state.get("spans", []))
        pages = list(state.get("pages", []) or [])
        hints_on = region_hints_enabled()
        schema_json, report = build_region_hints(
            dict(state.get("schema", {})),
            pages,
            spans if hints_on else None,
            _exclude_px_by_page(state) if hints_on else None,
        )
        result = kie_extract(
            adapter,
            bundle,
            spans=spans,
            layout_markdown=state.get("layout_markdown", ""),
            schema_json=schema_json,
            rule_hints=_rule_hints(state.get("active_rules", [])),
        )
        # 構造由来テーブル（structure_ocr が cell 座標付きで生成）を優先し、
        # 無い場合のみ LLM 抽出のテーブルを使う（§5.3: 座標/構造が正確）。
        tables = state.get("tables") or result.tables
        out: dict[str, Any] = {"fields": result.fields, "tables": tables}
        # ヒント無効時や領域を持たない schema では metrics に触らない（既存 run と
        # 1 バイトも変えない）。領域を評価した run だけ hints を丸ごと書き直す。
        if report:
            report.outcomes = dict(result.hint_outcomes)
            out["metrics"] = _merge_hint_metrics(state, report)
        return out

    return _node


def make_llm_correct(
    adapter: LLMAdapter, bundle: PromptBundle, *, low_conf_threshold: float = 0.80
) -> NodeFn:
    def _node(state: ExtractionState) -> dict[str, Any]:
        schema = FieldSchema.model_validate(state.get("schema", {"doc_type": "", "fields": []}))
        type_map = {f.name: f.type.value for f in schema.fields}
        spans_by_id: dict[int, Span] = {s.span_id: s for s in state.get("spans", [])}

        updated: list[ExtractedField] = []
        for field in state.get("fields", []):
            if field.confidence >= low_conf_threshold or not field.value_raw:
                updated.append(field)
                continue

            first = spans_by_id.get(field.span_ids[0]) if field.span_ids else None
            char_confs = first.char_confs if first else None
            result = llm_correct(
                adapter,
                bundle,
                field_name=field.name,
                field_type=type_map.get(field.name, "string"),
                fmt="",
                value_raw=field.value_raw,
                char_confs=char_confs,
                context=field.source_quote or "",
            )

            if result.applied and result.corrected is not None:
                field.correction = {
                    "applied": True,
                    "from": field.value_raw,
                    "to": result.corrected,
                    "by": "llm_correct",
                    "used_pairs": result.used_pairs,
                    "memory_refs": result.memory_refs,
                    "rationale": result.rationale,
                }
                # §12.1 correction_reuse_hits_total（価値 KPI）。
                # memory_refs が付いている＝過去の修正メモリを引いて直せた補正。
                # 「学習が実際に効いているか」を測る唯一の指標なので、適用が確定した
                # ここで数える（参照しただけ・採用されなかった補正は数えない）。
                if result.memory_refs:
                    correction_reuse_hits_total.labels(tenant=current_tenant()).inc()
                field.value_normalized = result.corrected
                field.confidence = apply_correction_confidence(
                    field.confidence, result.confidence, dd10_ok=True
                )
            elif result.needs_review:
                field.review_status = ReviewStatus.PENDING
                field.correction = {"applied": False, "needs_review": True}
            updated.append(field)
        return {"fields": updated}

    return _node
