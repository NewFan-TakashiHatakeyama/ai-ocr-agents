# mypy: ignore-errors
"""GoogleDriveProvider のトークンキャッシュ（レビュー確定major の回帰）。

単一インスタンスを全テナント・全接続で共有するため、キャッシュが refresh_token を
鍵にしないと別アカウントのアクセストークンを混用する（認可境界の破れ）。
"""

from __future__ import annotations

from typing import Any

from newfan_orchestrator.gdrive import GoogleDriveProvider


class _FakeResp:
    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, Any]:
        return self._body


def test_トークンはrefresh_tokenごとにキャッシュされる(monkeypatch) -> None:
    calls: list[str] = []

    def fake_post(url, data=None, timeout=None):
        calls.append(data["refresh_token"])
        return _FakeResp({"access_token": f"access-for-{data['refresh_token']}", "expires_in": 3600})

    import httpx

    monkeypatch.setattr(httpx, "post", fake_post)
    p = GoogleDriveProvider("cid", "csecret")

    # 接続A（refresh-X）→ 接続B（refresh-Y）の順で呼んでも混用しない
    assert p._access_token("refresh-X") == "access-for-refresh-X"
    assert p._access_token("refresh-Y") == "access-for-refresh-Y"
    # 2回目はキャッシュ（交換は各1回のみ）
    assert p._access_token("refresh-X") == "access-for-refresh-X"
    assert calls == ["refresh-X", "refresh-Y"]
