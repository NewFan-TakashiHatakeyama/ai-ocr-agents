from typing import Any

import httpx
import pytest

from newfan_paddle_client.client import PaddleServingClient, encode_image
from newfan_paddle_client.errors import PaddleServingError


def _mock_client(handler: Any) -> PaddleServingClient:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(base_url="http://structure-svc:8080", transport=transport)
    return PaddleServingClient("http://structure-svc:8080", client=http)


def test_layout_parsing_call(layout_parsing_raw: dict[str, Any]) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=layout_parsing_raw)

    client = _mock_client(handler)
    resp = client.layout_parsing(encode_image(b"fake-png"), file_type=1)

    assert captured["path"] == "/layout-parsing"
    # DD-01/ADR-0002: 既定で前処理オフ
    assert captured["body"]["useDocOrientationClassify"] is False
    assert captured["body"]["useDocUnwarping"] is False
    assert captured["body"]["useFormulaRecognition"] is False
    assert captured["body"]["fileType"] == 1
    assert len(resp.layout_parsing_results) == 1


def test_seal_flag_is_not_sent_by_default(layout_parsing_raw: dict[str, Any]) -> None:
    """印章はデプロイ単位のオプション。既定デプロイ（印章モデル未初期化）へ True を送ると 500。

    フラグを送らなければサーバの pipeline_config が正になり、structure-svc（false）/
    structure-svc-seal（true）を同一クライアントで切り替えられる（DD-03）。
    """
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=layout_parsing_raw)

    _mock_client(handler).layout_parsing(encode_image(b"fake-png"), file_type=1)
    assert "useSealRecognition" not in captured["body"]


def test_seal_flag_is_sent_when_explicit(layout_parsing_raw: dict[str, Any]) -> None:
    """印章デプロイ向けに明示指定は可能（None のときだけ省略）。"""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=layout_parsing_raw)

    _mock_client(handler).layout_parsing(
        encode_image(b"fake-png"), file_type=1, use_seal_recognition=True
    )
    assert captured["body"]["useSealRecognition"] is True


def test_envelope_error_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"logId": "x", "errorCode": 1, "errorMsg": "boom", "result": {}}
        )

    client = _mock_client(handler)
    with pytest.raises(PaddleServingError) as exc:
        client.ocr(encode_image(b"x"))
    assert exc.value.error_code == 1


def test_http_error_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    client = _mock_client(handler)
    with pytest.raises(PaddleServingError) as exc:
        client.layout_parsing(encode_image(b"x"))
    assert exc.value.status_code == 502
