import pytest

from newfan_llm_adapter import PromptBundle, default_bundle_dir


@pytest.fixture(scope="session")
def bundle() -> PromptBundle:
    return PromptBundle.load(default_bundle_dir())
