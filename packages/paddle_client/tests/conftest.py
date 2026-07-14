import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def layout_parsing_raw() -> dict[str, Any]:
    return _load("layout_parsing_response.json")


@pytest.fixture
def ocr_raw() -> dict[str, Any]:
    return _load("ocr_response.json")
