"""KIE 抽出（§4.6.1 / §5.5）。

LLM に span_ids 必須の JSON 契約で抽出させ、**span_ids の実在をコードで検証**する
（原文にない値・推測値を弾く安全策）。value_raw は LLM の value、source_quote は
実在 span テキストの連結（grounding 判定の根拠）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from newfan_schemas import ExtractedField, Span, TableCell, TableResult

from newfan_llm_adapter.adapter import LLMAdapter
from newfan_llm_adapter.bundle import PromptBundle, render
from newfan_llm_adapter.provider import LLMResponse


@dataclass
class KieResult:
    fields: list[ExtractedField] = field(default_factory=list)
    tables: list[TableResult] = field(default_factory=list)
    unmapped_required: list[str] = field(default_factory=list)
    response: LLMResponse | None = None


def _spans_for_prompt(spans: list[Span], *, with_bbox: bool = False) -> str:
    """span 一覧を JSON 化する。

    ``with_bbox`` は **読取領域のヒントを渡すときだけ** True にする。座標を常に載せると
    領域を使っていないスキーマまでプロンプトが膨らみ（span 1 件あたり 4 数値ぶん）、
    「領域機能を使っていない run の挙動は一切変わらない」という受け入れ条件も壊れる。
    ヒントが無ければ LLM は座標を照合しようがないので、載せる意味も無い。
    """
    if not with_bbox:
        return json.dumps(
            [
                {"span_id": s.span_id, "page": s.page, "text": s.text, "conf": round(s.conf, 3)}
                for s in spans
            ],
            ensure_ascii=False,
        )
    return json.dumps(
        [
            {
                "span_id": s.span_id,
                "page": s.page,
                "text": s.text,
                "conf": round(s.conf, 3),
                "bbox": list(s.bbox) if s.bbox else None,
            }
            for s in spans
        ],
        ensure_ascii=False,
    )


def _has_region_hint(schema_json: dict[str, Any]) -> bool:
    """スキーマに region_px（読取領域のヒント）を持つ field があるか。"""
    for f in schema_json.get("fields") or []:
        if isinstance(f, dict) and f.get("region_px"):
            return True
    return False


def _valid_span_ids(raw: Any, span_map: dict[int, Span]) -> list[int]:
    out: list[int] = []
    if isinstance(raw, list):
        for x in raw:
            if isinstance(x, int) and x in span_map:
                out.append(x)
    return out


def _page_and_bbox(
    valid_ids: list[int], span_map: dict[int, Span], fallback_page: Any
) -> tuple[Any, list[int] | None]:
    """根拠 span から field の (page, bbox) を決める（F-0, 設計 §7.2）。

    kie は長く span_ids しか付けず bbox を作らなかったため、テキスト項目の
    bbox は保存列があるのに常に NULL で、検証画面のオーバーレイに一切出なかった
    （明細だけ出ていたのは structure 解析がセル座標を持つため）。

    規則:
    1. 根拠 span が無ければ bbox を作らない。LLM 申告 page は残すが座標は捏造
       しない（原文に無い値を作らない span 根拠契約の延長）。
    2. 支配ページ = span 数が最多のページ。同数タイは (page, span_id) の辞書順
       最小。第一キーを page にするのは、span_id が読み順を表すのは単一
       build_spans 呼び出し内だけで、vl_fallback は全 OCR ページ確定後に
       max(span_id)+1 から採番するため（1 ページ目が VL・2 ページ目が OCR だと
       1 ページ目の id の方が大きくなり、span_id 優先では読み順と逆になる）。
    3. bbox は支配ページ上の span だけの外接矩形。ページを跨いで union すると
       別画像の座標が混ざり無意味な矩形になるため禁止。
    4. page は LLM 申告でなく span 由来で返す。申告 page は無検証で bbox と
       食い違い得るが、UI は page でフィルタするため誤ページに矩形が出る。
    """
    if not valid_ids:
        return fallback_page, None

    spans = [span_map[i] for i in valid_ids]
    by_page: dict[int, list[Span]] = {}
    for s in spans:
        by_page.setdefault(s.page, []).append(s)

    # 最多 → タイは (page, 最小 span_id) の辞書順
    page = min(
        by_page,
        key=lambda p: (-len(by_page[p]), p, min(s.span_id for s in by_page[p])),
    )
    boxes = [s.bbox for s in by_page[page] if s.bbox and len(s.bbox) >= 4]
    if not boxes:
        return page, None
    bbox = [
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    ]
    return page, bbox


def kie_extract(
    adapter: LLMAdapter,
    bundle: PromptBundle,
    *,
    spans: list[Span],
    layout_markdown: str,
    schema_json: dict[str, Any],
    rule_hints: str = "",
) -> KieResult:
    span_map = {s.span_id: s for s in spans}
    # スキーマから name→label を引く（HITL UI / §6.3 result の表示名）
    label_map = {
        f.get("name"): f.get("label")
        for f in (schema_json.get("fields") or [])
        if isinstance(f, dict)
    }
    system = "あなたは帳票からの項目抽出エンジンです。出力はJSONのみ。"
    with_hint = _has_region_hint(schema_json)
    user = render(
        bundle.kie_template,
        {
            "layout_markdown": layout_markdown,
            # 領域ヒントを渡すときだけ span にも座標を載せる（照合できるようにする）
            "spans": _spans_for_prompt(spans, with_bbox=with_hint),
            "schema_json": json.dumps(schema_json, ensure_ascii=False),
            "rule_hints": rule_hints,
        },
    )
    if with_hint:
        # 領域ヒントの説明は**ヒントを持つ run にだけ**足す。テンプレート本体に
        # 書くと、領域を使っていないテナントのプロンプトまで変わってしまう。
        user = user + bundle.kie_region_hint_template
    data, resp = adapter.complete_json(system=system, user=user, purpose="kie")

    result = KieResult(response=resp)
    seen_names: set[str] = set()
    for item in data.get("fields", []) or []:
        name = item.get("name")
        if not name:
            continue
        # 自動発見で LLM が同名を複数返すことがある（例: 合計が2つ）。DB は
        # UNIQUE (run_id, field_name) なので、素通しすると UPSERT の後勝ちで
        # 先の項目が無音消失する。サフィックスで別名化して両方を保全する
        # （どちらを残すかはレビュアが値を見て決められる）。
        if name in seen_names:
            n = 2
            while f"{name}_{n}" in seen_names:
                n += 1
            name = f"{name}_{n}"
        seen_names.add(name)
        valid_ids = _valid_span_ids(item.get("span_ids"), span_map)
        quote = " ".join(span_map[i].text for i in valid_ids) if valid_ids else None
        # 根拠 span から座標を合成する（F-0）。span 根拠契約を執行するこの場所に
        # 置く（根拠が無ければ bbox も作らない、を 1 箇所で守る）。
        page, bbox = _page_and_bbox(valid_ids, span_map, item.get("page"))
        # label の優先順位:
        # - スキーマ指定の抽出（label_map 非空）→ スキーマ定義のみを正とする。
        #   LLM 申告で上書きさせない（定義に label が無い項目も None のまま。
        #   ここで LLM 申告を混ぜると「スキーマ指定なのに表示名が実行ごとに揺れる」）
        # - スキーマレス自動発見（label_map 空）→ LLM 申告の見出し原文を使う。
        #   無検疫で通すと dict の repr・無制限長・制御文字（U+0000 は Pg の
        #   TEXT に入らず save_result ごと落ちる）まで流れるため、文字列のみ・
        #   制御文字除去・120 字で打ち切る
        label: str | None
        if label_map:
            label = label_map.get(item.get("name"))
        else:
            raw_label = item.get("label")
            if isinstance(raw_label, str) and raw_label.strip():
                cleaned = "".join(ch for ch in raw_label if ch.isprintable())
                label = cleaned.strip()[:120] or None
            else:
                label = None
        result.fields.append(
            ExtractedField(
                name=name,
                label=label,
                value_raw=(str(item["value"]) if item.get("value") is not None else None),
                span_ids=valid_ids,
                page=page,
                bbox=bbox,
                source_quote=quote,
            )
        )

    for table in data.get("tables", []) or []:
        rows: list[dict[str, TableCell]] = []
        for row in table.get("rows", []) or []:
            cells: dict[str, TableCell] = {}
            for col, cell in (row.get("cells", {}) or {}).items():
                cells[col] = TableCell(
                    value=(str(cell["value"]) if cell.get("value") is not None else None),
                    span_ids=_valid_span_ids(cell.get("span_ids"), span_map),
                )
            rows.append(cells)
        result.tables.append(TableResult(name=table.get("name", "table"), rows=rows))

    result.unmapped_required = [str(x) for x in (data.get("unmapped_required") or [])]
    return result
