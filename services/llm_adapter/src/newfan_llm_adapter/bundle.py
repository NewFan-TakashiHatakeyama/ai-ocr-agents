"""プロンプトバンドル読込（§2.4 / §4.6）。prompts/{version}/ の YAML 群を読む。

テンプレート内には出力例の JSON（`{"fields":...}`）が含まれるため、str.format は使えない。
`{word}` 形式の単語プレースホルダのみ安全に置換する（JSON の `{"..."` は非対象）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def render(template: str, mapping: dict[str, str]) -> str:
    def _sub(m: re.Match[str]) -> str:
        key = m.group(1)
        return mapping.get(key, m.group(0))

    return _PLACEHOLDER.sub(_sub, template)


def _confusion_set(data: dict[str, Any]) -> list[frozenset[str]]:
    groups: list[frozenset[str]] = []
    for section in ("digits_alpha", "symbols", "kana_kanji"):
        for group in data.get(section, []) or []:
            groups.append(frozenset(str(x) for x in group))
    return groups


@dataclass
class PromptBundle:
    version: str
    kie_template: str
    correct_template: str
    rule_extract_template: str
    confusion_groups: list[frozenset[str]]
    thresholds: dict[str, Any]

    @classmethod
    def load(cls, bundle_dir: Path) -> "PromptBundle":
        def _read_template(name: str) -> str:
            doc = yaml.safe_load((bundle_dir / name).read_text(encoding="utf-8"))
            return str(doc.get("template", ""))

        meta = yaml.safe_load((bundle_dir / "bundle.yaml").read_text(encoding="utf-8"))
        confusion = yaml.safe_load(
            (bundle_dir / "confusion_pairs.yaml").read_text(encoding="utf-8")
        )
        thresholds = yaml.safe_load(
            (bundle_dir / "thresholds.yaml").read_text(encoding="utf-8")
        )
        return cls(
            version=str(meta.get("version", "")),
            kie_template=_read_template("kie_extract.yaml"),
            correct_template=_read_template("llm_correct.yaml"),
            rule_extract_template=_read_template("rule_extract.yaml"),
            confusion_groups=_confusion_set(confusion),
            thresholds=thresholds or {},
        )

    def confusion_allows(self, pair: tuple[str, str]) -> bool:
        """混同文字表が pair（2文字）の相互置換を許可するか（DD-10）。"""
        a, b = pair
        return any(a in g and b in g for g in self.confusion_groups)


def default_bundle_dir(version: str = "2026.07-1") -> Path:
    """リポジトリの prompts/{version} を上方探索で見つける。"""
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "prompts" / version
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError(f"prompts/{version} が見つかりません")
