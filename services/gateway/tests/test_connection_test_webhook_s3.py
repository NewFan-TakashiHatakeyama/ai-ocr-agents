"""webhook / s3 接続の疎通テスト（POST /connections/{id}/test, §16.5）。

これが無いと webhook/s3 は永久に untested で、lint L010 が有効化を拒み続ける
（UI から抜けられない）。契約の要点:
- webhook: 本配信（newfan_export.webhook）と**同じ署名・同じヘッダ**で
  {"event":"test"} を 1 回送る。SSRF ガードを通し、2xx で tested
- 失敗（非 2xx・ネットワーク・URL 拒否・バケット不在）は 422 で理由を返し、
  status は untested のまま。内部例外の文言は外に出さない
- 成功すると既存の lint 経路で L010 が消える（connection_ok は status を見る）
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient

from newfan_gateway import conntest
from newfan_gateway.admin import InMemoryAdminRepository
from newfan_gateway.app import create_app
from newfan_gateway.config import Settings
from newfan_gateway.ports import FakeSecretStore
from newfan_gateway.workflows_repo import InMemoryWorkflowsRepository

SECRET = "test-secret-0123456789-abcdefghijklmnop"
# 公開 IP リテラル: SSRF ガードが DNS を引かずに通す（テストをネットワークに依存させない）
HOOK_URL = "https://93.184.216.34/hook"
HOOK_SECRET_REF = "arn:fake:ai-ocr/test/conn/ten_1/hook"


def _auth(role: str = "admin") -> dict[str, str]:
    tok = jwt.encode({"sub": "u1", "tenant_id": "ten_1", "role": role}, SECRET, algorithm="HS256")
    return {"Authorization": f"Bearer {tok}"}


class Env:
    def __init__(self) -> None:
        self.admin = InMemoryAdminRepository()
        # 接続の実体を渡す = connection_ok が Pg と同じ規則（status IN active/tested）で判定
        self.workflows = InMemoryWorkflowsRepository(admin=self.admin)
        self.workflows.seed_schema_id("ten_1", "sch_inv")
        self.secret_store = FakeSecretStore()
        self.secret_store.values[HOOK_SECRET_REF] = "hook-signing-key"
        self.client = TestClient(
            create_app(
                settings=Settings(jwt_secret=SECRET),
                admin=self.admin,
                workflows=self.workflows,
                secret_store=self.secret_store,
            )
        )

    def create(self, **kw: Any) -> dict[str, Any]:
        r = self.client.post("/v1/connections", json=kw, headers=_auth())
        assert r.status_code == 201, r.text
        return r.json()  # type: ignore[no-any-return]

    def test_conn(self, cid: str) -> httpx.Response:
        return self.client.post(f"/v1/connections/{cid}/test", headers=_auth())

    def status_of(self, cid: str) -> str:
        items = self.client.get("/v1/connections", headers=_auth()).json()["items"]
        return next(str(i["status"]) for i in items if i["id"] == cid)


@pytest.fixture
def env() -> Env:
    return Env()


def _mock_http(monkeypatch: pytest.MonkeyPatch, handler: Any) -> list[httpx.Request]:
    """疎通テストの httpx.Client を MockTransport に差し替え、届いた要求を記録する。"""
    seen: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)  # type: ignore[no-any-return]

    monkeypatch.setattr(
        conntest, "new_http_client",
        lambda timeout: httpx.Client(transport=httpx.MockTransport(_handler), timeout=timeout),
    )
    return seen


# ---------- webhook ----------


def test_webhookは本配信と同じ署名で送り2xxならtestedになる(env: Env, monkeypatch) -> None:
    seen = _mock_http(monkeypatch, lambda r: httpx.Response(204))
    c = env.create(type="webhook", name="hook", config={"url": HOOK_URL},
                   secret_ref=HOOK_SECRET_REF)
    assert c["status"] == "untested"

    r = env.test_conn(c["id"])
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "status": "tested", "message": None}
    assert env.status_of(c["id"]) == "tested"

    # 1 回だけ、本配信と同じヘッダ・同じ署名関数（newfan_export.webhook.sign と一致）
    from newfan_export.webhook import sign as export_sign

    assert len(seen) == 1
    req = seen[0]
    assert str(req.url) == HOOK_URL
    assert req.headers["Content-Type"] == "application/json"
    assert req.headers["X-NF-Signature"] == export_sign(req.content, "hook-signing-key")
    assert req.headers["X-NF-Timestamp"].isdigit()
    body = json.loads(req.content)
    assert body["event"] == "test" and body["connection_id"] == c["id"]
    assert body["tenant_id"] == "ten_1" and body["occurred_at"]
    assert any(a["action"] == "connection.test" and a["detail"]["ok"] for a in env.workflows.audits)


def test_webhookが非2xxを返したら422で理由を返しuntestedのまま(env: Env, monkeypatch) -> None:
    _mock_http(monkeypatch, lambda r: httpx.Response(500, text="boom"))
    c = env.create(type="webhook", name="hook", config={"url": HOOK_URL},
                   secret_ref=HOOK_SECRET_REF)
    r = env.test_conn(c["id"])
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "E4001"
    assert "HTTP 500" in err["message"]
    assert err["details"]["status_code"] == 500 and err["details"]["type"] == "webhook"
    assert env.status_of(c["id"]) == "untested"
    assert any(
        a["action"] == "connection.test" and a["detail"] == {"ok": False, "reason": err["message"]}
        for a in env.workflows.audits
    )


def test_webhookのネットワーク例外は内部文言を出さず422(env: Env, monkeypatch) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[Errno 111] internal detail: proxy=http://corp-proxy:3128")

    _mock_http(monkeypatch, boom)
    c = env.create(type="webhook", name="hook", config={"url": HOOK_URL},
                   secret_ref=HOOK_SECRET_REF)
    r = env.test_conn(c["id"])
    assert r.status_code == 422, r.text
    msg = r.json()["error"]["message"]
    assert "接続できません" in msg
    assert "corp-proxy" not in r.text and "Errno" not in r.text
    assert env.status_of(c["id"]) == "untested"


def test_webhookのタイムアウトは422(env: Env, monkeypatch) -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    _mock_http(monkeypatch, slow)
    c = env.create(type="webhook", name="hook", config={"url": HOOK_URL},
                   secret_ref=HOOK_SECRET_REF)
    r = env.test_conn(c["id"])
    assert r.status_code == 422
    assert "応答しませんでした" in r.json()["error"]["message"]


def test_SSRFに掛かるURLは送信せずに422(env: Env, monkeypatch) -> None:
    seen = _mock_http(monkeypatch, lambda r: httpx.Response(200))
    # 内部ネットワーク宛て（メタデータ）・localhost はガードで止める。1 バイトも送らない
    for url in ("http://169.254.169.254/latest/meta-data", "http://localhost/hook"):
        c = env.create(type="webhook", name="hook", config={"url": url}, secret_ref=HOOK_SECRET_REF)
        r = env.test_conn(c["id"])
        assert r.status_code == 422, r.text
        assert "拒否" in r.json()["error"]["message"]
        assert env.status_of(c["id"]) == "untested"
    assert seen == []


def test_webhookはurl未設定と秘密未解決を422で区別する(env: Env, monkeypatch) -> None:
    seen = _mock_http(monkeypatch, lambda r: httpx.Response(200))
    c = env.create(type="webhook", name="hook", config={})
    r = env.test_conn(c["id"])
    assert r.status_code == 422 and "config.url" in r.json()["error"]["message"]

    c2 = env.create(type="webhook", name="hook", config={"url": HOOK_URL},
                    secret_ref="arn:fake:ai-ocr/test/conn/ten_1/missing")
    r = env.test_conn(c2["id"])
    assert r.status_code == 422 and "secret_ref" in r.json()["error"]["message"]
    assert seen == []


def test_旧行のconfig_secretでも署名して送れる(env: Env, monkeypatch) -> None:
    # P6 以前の webhook 行は config.secret に平文鍵が残る（本配信も fallback で読む）
    seen = _mock_http(monkeypatch, lambda r: httpx.Response(200))
    rec = env.admin.add_webhook_endpoint("ten_1", url=HOOK_URL, secret="legacy-key", name="old")
    r = env.test_conn(rec.id)
    assert r.status_code == 200 and r.json()["ok"] is True
    from newfan_netguard import sign

    assert seen[0].headers["X-NF-Signature"] == sign(seen[0].content, "legacy-key")


# ---------- s3 ----------


class _FakeS3:
    def __init__(self, exc: Exception | None = None) -> None:
        self.exc = exc
        self.calls: list[dict[str, Any]] = []

    def head_bucket(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(kw)
        if self.exc is not None:
            raise self.exc
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}


def _mock_boto3(monkeypatch: pytest.MonkeyPatch, fake: _FakeS3) -> list[tuple[Any, ...]]:
    import boto3

    made: list[tuple[Any, ...]] = []

    def _client(*args: Any, **kwargs: Any) -> _FakeS3:
        made.append(args)
        return fake

    monkeypatch.setattr(boto3, "client", _client)
    return made


def _client_error(code: str, status: int) -> Exception:
    from botocore.exceptions import ClientError

    return ClientError(
        {"Error": {"Code": code, "Message": "x"}, "ResponseMetadata": {"HTTPStatusCode": status}},
        "HeadBucket",
    )


def test_s3はHeadBucket成功でtestedになる(env: Env, monkeypatch) -> None:
    fake = _FakeS3()
    made = _mock_boto3(monkeypatch, fake)
    c = env.create(type="s3", name="inbox", config={"bucket": "ai-ocr-inbox-123", "prefix": "in/"})
    r = env.test_conn(c["id"])
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True and env.status_of(c["id"]) == "tested"
    assert made == [("s3",)]  # sink（S3FileWriter）と同じ boto3.client("s3")
    assert fake.calls == [{"Bucket": "ai-ocr-inbox-123"}]


def test_s3はバケット不在と権限不足と認証情報無しを区別して422(env: Env, monkeypatch) -> None:
    from botocore.exceptions import NoCredentialsError

    cases = [
        (_client_error("404", 404), "存在しません"),
        (_client_error("NoSuchBucket", 404), "存在しません"),
        (_client_error("403", 403), "アクセス権"),
        (NoCredentialsError(), "認証情報"),
    ]
    for exc, expected in cases:
        _mock_boto3(monkeypatch, _FakeS3(exc))
        c = env.create(type="s3", name="inbox", config={"bucket": "nope"})
        r = env.test_conn(c["id"])
        assert r.status_code == 422, r.text
        assert expected in r.json()["error"]["message"], (exc, r.text)
        assert r.json()["error"]["details"]["bucket"] == "nope"
        assert env.status_of(c["id"]) == "untested"


def test_s3はbucket未設定なら呼ばずに422(env: Env, monkeypatch) -> None:
    fake = _FakeS3()
    _mock_boto3(monkeypatch, fake)
    c = env.create(type="s3", name="inbox", config={})
    r = env.test_conn(c["id"])
    assert r.status_code == 422 and "config.bucket" in r.json()["error"]["message"]
    assert fake.calls == []


# ---------- フォルダ監視系は対象外（「今すぐ同期」が兼ねる） ----------


def test_フォルダ監視系はE1005で今すぐ同期へ誘導(env: Env) -> None:
    c = env.create(type="gdrive", name="drive", config={"folder_id": "abc"})
    r = env.test_conn(c["id"])
    assert r.status_code == 409
    assert "今すぐ同期" in r.json()["error"]["message"]


# ---------- lint L010 ----------


def _graph(sink: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": 1,
        "nodes": [
            {"id": "t1", "type": "source.manual", "config": {}},
            {"id": "x1", "type": "process.extract", "config": {"schema_id": "sch_inv"}},
            {"id": "s1", **sink},
        ],
        "edges": [{"from": "t1", "to": "x1"}, {"from": "x1", "to": "s1"}],
    }


def _lint(env: Env, graph: dict[str, Any]) -> dict[str, Any]:
    # auto_confirm=true: HITL 分岐が無いことの警告（L008）を出さず L010 だけを見る
    r = env.client.post(
        "/v1/workflows",
        json={"name": "wf", "graph_json": graph, "auto_confirm": True},
        headers=_auth(),
    )
    assert r.status_code == 201, r.text
    r = env.client.post(f"/v1/workflows/{r.json()['id']}/lint", headers=_auth())
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


def test_疎通テスト成功でwebhookとs3のL010が消える(env: Env, monkeypatch) -> None:
    _mock_http(monkeypatch, lambda r: httpx.Response(200))
    _mock_boto3(monkeypatch, _FakeS3())
    hook = env.create(type="webhook", name="hook", config={"url": HOOK_URL},
                      secret_ref=HOOK_SECRET_REF)
    s3 = env.create(type="s3", name="inbox", config={"bucket": "b"})
    hook_graph = _graph({"type": "sink.webhook", "config": {"connection_id": hook["id"]}})
    s3_graph = _graph({
        "type": "sink.file",
        "config": {"connection_id": s3["id"], "path": "out/{workflow_run_id}.json"},
    })

    # 登録直後（untested）は既存の lint 経路が L010（error）を出し、有効化できない
    for g in (hook_graph, s3_graph):
        body = _lint(env, g)
        assert [f["rule"] for f in body["findings"]] == ["L010"], body
        assert body["activatable"] is False

    assert env.test_conn(hook["id"]).status_code == 200
    assert env.test_conn(s3["id"]).status_code == 200

    for g in (hook_graph, s3_graph):
        body = _lint(env, g)
        assert body["findings"] == [] and body["activatable"] is True, body
