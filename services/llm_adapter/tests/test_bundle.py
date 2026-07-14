from newfan_llm_adapter import PromptBundle
from newfan_llm_adapter.bundle import render


def test_bundle_loads(bundle: PromptBundle) -> None:
    assert bundle.version == "2026.07-1"
    assert "項目抽出エンジン" in bundle.kie_template
    assert "校正者" in bundle.correct_template
    assert bundle.confusion_groups


def test_confusion_allows(bundle: PromptBundle) -> None:
    # 付録A: 1↔7 は同一グループ
    assert bundle.confusion_allows(("1", "7")) is True
    assert bundle.confusion_allows(("O", "0")) is True
    # 無関係なペアは不許可
    assert bundle.confusion_allows(("1", "9")) is False


def test_render_preserves_json_braces() -> None:
    template = 'set {name}\nexample: {"fields":[{"x":1}]}'
    out = render(template, {"name": "invoice"})
    assert "set invoice" in out
    # JSON の波括弧は温存される
    assert '{"fields":[{"x":1}]}' in out
