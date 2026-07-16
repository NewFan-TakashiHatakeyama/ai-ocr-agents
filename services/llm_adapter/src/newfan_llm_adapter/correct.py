"""LLM 補正（§4.6.2, DD-10）。

DD-10 の制約（自動置換は「混同文字表の組合せ」または「テナント修正メモリの類似例に合致」
する場合のみ）を**コードで強制**する。LLM の needs_review 自己申告に依存しない。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

from newfan_llm_adapter.adapter import LLMAdapter
from newfan_llm_adapter.bundle import PromptBundle, render
from newfan_llm_adapter.provider import LLMResponse


@dataclass
class CorrectionResult:
    corrected: Optional[str]
    applied: bool  # 自動適用してよいか（DD-10 適合かつ changed）
    needs_review: bool
    used_pairs: list[list[str]]
    memory_refs: list[str]
    rationale: str
    confidence: float
    response: LLMResponse | None = None


def _flatten_confusion(bundle: PromptBundle) -> str:
    pairs: list[list[str]] = []
    for group in bundle.confusion_groups:
        members = sorted(group)
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                pairs.append([members[i], members[j]])
    return json.dumps(pairs, ensure_ascii=False)


def _dd10_ok(
    bundle: PromptBundle, used_pairs: list[list[str]], memory_refs: list[str]
) -> bool:
    # メモリ類似例に合致（memory_refs あり）なら許可
    if memory_refs:
        return True
    # そうでなければ、全ての used_pairs が混同文字表に含まれること
    if not used_pairs:
        return False
    for pair in used_pairs:
        if len(pair) != 2 or not bundle.confusion_allows((str(pair[0]), str(pair[1]))):
            return False
    return True


def llm_correct(
    adapter: LLMAdapter,
    bundle: PromptBundle,
    *,
    field_name: str,
    field_type: str,
    fmt: str,
    value_raw: str,
    char_confs: list[float] | None,
    context: str,
    few_shot_examples: list[dict[str, Any]] | None = None,
) -> CorrectionResult:
    system = "あなたはOCR結果の校正者です。視覚的に妥当な範囲でのみ修正します。出力はJSONのみ。"
    user = render(
        bundle.correct_template,
        {
            "field_name": field_name,
            "field_type": field_type,
            "format": fmt,
            "value_raw": value_raw,
            "char_confs": json.dumps(char_confs or [], ensure_ascii=False),
            "context": context,
            "few_shot_examples": json.dumps(few_shot_examples or [], ensure_ascii=False),
            "confusion_pairs": _flatten_confusion(bundle),
        },
    )
    data, resp = adapter.complete_json(system=system, user=user, purpose="correct")

    corrected = data.get("corrected")
    changed = bool(data.get("changed", False))
    used_pairs = [list(map(str, p)) for p in (data.get("used_pairs") or [])]
    memory_refs = [str(x) for x in (data.get("memory_refs") or [])]

    # DD-10 をコードで強制。違反 or 未変更なら自動適用しない。
    dd10 = _dd10_ok(bundle, used_pairs, memory_refs)
    applied = changed and dd10
    needs_review = bool(data.get("needs_review", False)) or (changed and not dd10)

    return CorrectionResult(
        corrected=(str(corrected) if corrected is not None else None),
        applied=applied,
        needs_review=needs_review,
        used_pairs=used_pairs,
        memory_refs=memory_refs,
        rationale=str(data.get("rationale", "")),
        confidence=float(data.get("confidence", 0.0)),
        response=resp,
    )
