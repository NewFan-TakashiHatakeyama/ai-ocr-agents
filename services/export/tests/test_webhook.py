import hashlib
import hmac

import httpx

from newfan_export import ExportError, WebhookSender, build_event, is_blocked_url, next_retry_delay, sign
from newfan_export.webhook import RETRY_SCHEDULE_SEC

_PUBLIC_RESOLVER = lambda host: ["93.184.216.34"]  # noqa: E731


def test_sign_matches_hmac() -> None:
    body = b'{"a":1}'
    expected = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert sign(body, "secret") == expected


def test_next_retry_schedule() -> None:
    assert [next_retry_delay(i) for i in range(5)] == RETRY_SCHEDULE_SEC
    assert next_retry_delay(5) is None


def test_is_blocked_url_private_and_loopback() -> None:
    assert is_blocked_url("http://127.0.0.1/hook") is True
    assert is_blocked_url("http://10.0.0.5/hook") is True
    assert is_blocked_url("http://169.254.1.1/hook") is True  # link-local
    assert is_blocked_url("http://localhost/hook") is True
    assert is_blocked_url("ftp://example.com") is True  # 非http(s)


def test_is_blocked_url_public_allowed() -> None:
    assert is_blocked_url("http://93.184.216.34/hook") is False
    assert is_blocked_url("https://example.com/hook", resolver=_PUBLIC_RESOLVER) is False


def test_sender_signs_and_posts() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["sig"] = request.headers.get("X-NF-Signature")
        captured["ts"] = request.headers.get("X-NF-Timestamp")
        captured["body"] = request.content
        return httpx.Response(200)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    sender = WebhookSender(client=client, resolver=_PUBLIC_RESOLVER)
    event = build_event("document.confirmed", tenant_id="t", document_id="d", run_id="r", data={"x": 1})

    ok = sender.send("https://example.com/hook", "secret", event)
    assert ok is True
    # 署名がボディに一致
    assert captured["sig"] == sign(captured["body"], "secret")
    assert captured["ts"]


def test_sender_blocks_ssrf() -> None:
    sender = WebhookSender(client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))))
    event = build_event("document.confirmed", tenant_id="t", document_id="d", run_id="r", data={})
    try:
        sender.send("http://169.254.169.254/latest/meta-data", "s", event)
        raise AssertionError("SSRF がブロックされていない")
    except ExportError as exc:
        assert exc.code == "E5001"


def test_sender_returns_false_on_5xx() -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(502)))
    sender = WebhookSender(client=client, resolver=_PUBLIC_RESOLVER)
    event = build_event("document.confirmed", tenant_id="t", document_id="d", run_id="r", data={})
    assert sender.send("https://example.com/hook", "s", event) is False
