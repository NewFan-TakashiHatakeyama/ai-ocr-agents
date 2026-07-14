"""llm-adapter: LLM 抽象・KIE/補正実行（§4.6 / §5.5, DD-10）。"""

from newfan_llm_adapter.adapter import LLMAdapter, estimate_cost_usd
from newfan_llm_adapter.bundle import PromptBundle, default_bundle_dir, render
from newfan_llm_adapter.correct import CorrectionResult, llm_correct
from newfan_llm_adapter.errors import LLMError
from newfan_llm_adapter.kie import KieResult, kie_extract
from newfan_llm_adapter.provider import FakeProvider, LLMProvider, LLMResponse

__all__ = [
    "LLMAdapter",
    "estimate_cost_usd",
    "PromptBundle",
    "default_bundle_dir",
    "render",
    "kie_extract",
    "KieResult",
    "llm_correct",
    "CorrectionResult",
    "LLMError",
    "LLMProvider",
    "LLMResponse",
    "FakeProvider",
]
