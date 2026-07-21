"""§6.2 の未実装だった 2 本（修正メモリ照会 / Webhook 配信先登録）。

- GET /tenants/{id}/memory: DD-06/DD-07 の学習内容を人が確認する唯一の手段。
- POST /webhooks/endpoints: export は connections から配信先を読むが、そこへ
  行を入れる API が無く DB 直投入しかなかった。
"""

from __future__ import annotations

import jwt
import pytest
from fastapi.testclient import TestClient

from newfan_gateway.admin import InMemoryAdminRepository
from newfan_gateway.app import create_app
from newfan_gateway.config import Settings
from newfan_gateway.records import MemoryRecord

SECRET = "test-secret-0123456789-abcdefghijklmnop"


def _token(role: str = "admin", tenant: str = "ten_1") -> str:
    return jwt.encode({"sub": "u", "tenant_id": tenant, "role": role}, SECRET, algorithm="HS256")


def _auth(role: str = "admin", tenant: str = "ten_1") -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(role, tenant)}"}


@pytest.fixture
def admin() -> InMemoryAdminRepository:
    a = InMemoryAdminRepository()
    a.seed_memory(
        MemoryRecord(
            id="mem_1", tenant_id="ten_1", correction_log_id="cor_1", embed_model="e5-small",
            field_name="total_amount", original_value="I36998", corrected_value="136998",
            doc_type="invoice", supplier_key="ＡＡＡ食品", created_at="2026-07-16T00:00:00Z",
        )
    )
    a.seed_memory(
        MemoryRecord(
            id="mem_2", tenant_id="ten_1", correction_log_id="cor_2", embed_model="e5-small",
            field_name="issuer_name", original_value="サンフル商事", corrected_value="サンプル商事",
            doc_type="quotation", created_at="2026-07-16T01:00:00Z",
        )
    )
    a.seed_memory(
        MemoryRecord(
            id="mem_other", tenant_id="ten_2", correction_log_id="cor_9", embed_model="e5-small",
            field_name="total_amount", corrected_value="999", created_at="2026-07-16T02:00:00Z",
        )
    )
    return a


@pytest.fixture
def client(admin: InMemoryAdminRepository) -> TestClient:
    app = create_app(settings=Settings(jwt_secret=SECRET), admin=admin)
    return TestClient(app)


# --- GET /tenants/{id}/memory ---


def test_修正メモリを新しい順で返す(client: TestClient) -> None:
    r = client.get("/v1/tenants/ten_1/memory", headers=_auth())
    assert r.status_code == 200
    items = r.json()["items"]
    assert [i["id"] for i in items] == ["mem_2", "mem_1"]
    assert items[1]["original_value"] == "I36998"
    assert items[1]["corrected_value"] == "136998"


def test_doc_typeとfield_nameで絞れる(client: TestClient) -> None:
    r = client.get("/v1/tenants/ten_1/memory", params={"doc_type": "invoice"}, headers=_auth())
    assert [i["id"] for i in r.json()["items"]] == ["mem_1"]
    r = client.get(
        "/v1/tenants/ten_1/memory", params={"field_name": "issuer_name"}, headers=_auth()
    )
    assert [i["id"] for i in r.json()["items"]] == ["mem_2"]


def test_他テナントのメモリは参照できない(client: TestClient) -> None:
    # admin はテナント内の権限。パスに他テナントを書けば見える、では RLS を迂回する穴になる。
    r = client.get("/v1/tenants/ten_2/memory", headers=_auth(tenant="ten_1"))
    assert r.status_code == 403  # §6.5 E5001=権限不足
    assert r.json()["error"]["code"] == "E5001"


def test_メモリ照会はadmin限定(client: TestClient) -> None:
    r = client.get("/v1/tenants/ten_1/memory", headers=_auth(role="reviewer"))
    assert r.status_code == 403


def test_limitは上限で頭打ちになる(client: TestClient) -> None:
    # 無制限に引けると DB とレスポンスが破裂する
    r = client.get("/v1/tenants/ten_1/memory", params={"limit": 100000}, headers=_auth())
    assert r.status_code == 200


# --- POST /webhooks/endpoints ---


def test_webhook配信先を登録できる(client: TestClient) -> None:
    r = client.post(
        "/v1/webhooks/endpoints",
        json={"url": "https://example.com/hook", "name": "基幹連携"},
        headers=_auth(),
    )
    assert r.status_code == 201
    body = r.json()
    assert body["url"] == "https://example.com/hook"
    # 登録しただけでは配信しない。export は active/tested しか拾わない（§16.5）。
    assert body["status"] == "untested"
    assert body["secret"], "署名鍵が返っていない（§6.4 の検証ができない）"


def test_secret省略時はサーバが生成する(client: TestClient) -> None:
    # 利用者に鍵を選ばせると "test" のような弱い鍵が使われ、署名検証が意味を失う。
    # URL は解決できるホストにする（SSRF ガードは名前解決できない host も拒否するため、
    # a.example のような架空ドメインだと鍵生成まで到達しない）。
    r1 = client.post(
        "/v1/webhooks/endpoints", json={"url": "https://example.com/h1"}, headers=_auth()
    )
    r2 = client.post(
        "/v1/webhooks/endpoints", json={"url": "https://example.com/h2"}, headers=_auth()
    )
    s1, s2 = r1.json()["secret"], r2.json()["secret"]
    assert len(s1) >= 32
    assert s1 != s2


def test_一覧ではsecretを返さない(client: TestClient) -> None:
    client.post("/v1/webhooks/endpoints", json={"url": "https://example.com/hook"}, headers=_auth())
    r = client.get("/v1/webhooks/endpoints", headers=_auth())
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["secret"] is None, "署名鍵を読み出せてはいけない"


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8000/hook",
        "http://127.0.0.1/hook",
        "http://169.254.169.254/latest/meta-data/",  # EC2 メタデータ
        "http://10.0.0.5/hook",
        "file:///etc/passwd",
        "ftp://example.com/hook",
    ],
)
def test_内部宛や非httpのURLは登録を拒否する(client: TestClient, url: str) -> None:
    # 送信時にも弾いているが、そこで初めて落ちると利用者には「なぜか届かない」としか
    # 見えない。登録時に理由を返す。
    r = client.post("/v1/webhooks/endpoints", json={"url": url}, headers=_auth())
    assert r.status_code == 403  # §6.5 E5001=権限不足（403）
    assert r.json()["error"]["code"] == "E5001"


def test_webhook登録はadmin限定(client: TestClient) -> None:
    r = client.post(
        "/v1/webhooks/endpoints", json={"url": "https://example.com/h"}, headers=_auth(role="reviewer")
    )
    assert r.status_code == 403
