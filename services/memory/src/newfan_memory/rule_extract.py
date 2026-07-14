"""ルール抽出（§4.6.3）。修正ログから draft ルールを LLM で抽出する。"""

from __future__ import annotations

import json
from typing import Any

from newfan_llm_adapter import LLMAdapter, PromptBundle
from newfan_llm_adapter.bundle import render

from newfan_memory.records import CorrectionLog, RuleType


def _corrections_for_prompt(corrections: list[CorrectionLog]) -> str:
    return json.dumps(
        [
            {
                "field": c.field_name,
                "from": c.original_value,
                "to": c.corrected_value,
                "doc_type": c.doc_type,
                "supplier": c.supplier_key,
                "context": c.context,
            }
            for c in corrections
        ],
        ensure_ascii=False,
    )


def extract_rules(
    adapter: LLMAdapter, bundle: PromptBundle, corrections: list[CorrectionLog]
) -> list[dict[str, Any]]:
    """LLM に §4.6.3 プロンプトで抽出させ、rule dict のリストを返す（正規化はサービス側）。"""
    system = "あなたはルール抽出器です。出力はJSON配列のみ。"
    user = render(bundle.rule_extract_template, {"corrections": _corrections_for_prompt(corrections)})
    data, _ = adapter.complete_json(system=system, user=user)
    if not isinstance(data, list):
        return []
    out: list[dict[str, Any]] = []
    valid_types = {t.value for t in RuleType}
    for item in data:
        if isinstance(item, dict) and item.get("rule_type") in valid_types:
            out.append(item)
    return out
