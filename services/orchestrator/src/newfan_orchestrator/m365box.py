# mypy: ignore-errors
"""Microsoft 365（Graph）と Box のフォルダプロバイダ（⑤⑥ 横展開）。

GDrive（gdrive.py）と同じ Protocol（list_files/download）を実装する。
方針はモックファースト: 開発は FakeGDriveProvider（ローカル dir）を全種別で共用し、
実 API は env が揃った時のみ worker_main が有効化する。実アカウントでの E2E は未了。

トークンキャッシュは必ず資格情報を鍵にする（gdrive の敵対的レビューで、鍵なしの
インスタンスキャッシュが別アカウントのトークンを混用する major を確定させた教訓）。

- GraphFolderProvider: アプリ専用（client credentials）フロー。folder_id は
  "<drive_id>/<item_id>"（例 "b!abc.../01ABC..."）。OneDrive/SharePoint の
  ドライブ配下フォルダを列挙する。
- BoxFolderProvider: Client Credentials Grant（enterprise）。folder_id は Box の
  フォルダ ID（数値文字列）。
"""

from __future__ import annotations

from typing import Optional

from newfan_orchestrator.gdrive import DriveFile


class GraphFolderProvider:
    """Microsoft Graph（OneDrive/SharePoint）。アプリ専用トークン。

    認可: env の M365_TENANT_ID / M365_CLIENT_ID / M365_CLIENT_SECRET による
    client credentials。接続毎の secret（委任トークン）は現状使わない
    （secret を渡された場合も無視せず将来の委任フローに残す）。
    """

    API = "https://graph.microsoft.com/v1.0"

    def __init__(
        self, tenant_id: str, client_id: str, client_secret: str, *, timeout: float = 30.0
    ) -> None:
        self._tenant_id = tenant_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._timeout = timeout
        # 資格情報タプル → (token, expires_at)。単一アプリでも鍵付きで持つ（教訓の固定化）
        self._tokens: dict[tuple[str, str], tuple[str, float]] = {}

    def _access_token(self) -> str:
        import time

        import httpx

        key = (self._tenant_id, self._client_id)
        cached = self._tokens.get(key)
        if cached and time.monotonic() < cached[1] - 60:
            return cached[0]
        resp = httpx.post(
            f"https://login.microsoftonline.com/{self._tenant_id}/oauth2/v2.0/token",
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "scope": "https://graph.microsoft.com/.default",
                "grant_type": "client_credentials",
            },
            timeout=self._timeout,
        )
        resp.raise_for_status()
        body = resp.json()
        token = str(body["access_token"])
        self._tokens[key] = (token, time.monotonic() + float(body.get("expires_in", 3600)))
        return token

    @staticmethod
    def _split(folder_id: str) -> tuple[str, str]:
        drive_id, _, item_id = folder_id.partition("/")
        if not drive_id or not item_id:
            raise ValueError(
                f"m365 の folder_id は '<drive_id>/<item_id>' 形式で指定してください: {folder_id!r}"
            )
        return drive_id, item_id

    def list_files(self, *, folder_id: str, secret: Optional[str]) -> list[DriveFile]:
        import httpx

        drive_id, item_id = self._split(folder_id)
        token = self._access_token()
        files: list[DriveFile] = []
        url = f"{self.API}/drives/{drive_id}/items/{item_id}/children"
        params: dict[str, str] = {
            "$select": "id,name,file,cTag,lastModifiedDateTime",
            "$top": "100",
        }
        while url:
            resp = httpx.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {self._access_token()}"},
                timeout=self._timeout,
            )
            resp.raise_for_status()
            body = resp.json()
            for it in body.get("value", []):
                if "file" not in it:
                    continue  # サブフォルダは対象外（S3 prefix 同様、直下のみ）
                files.append(
                    DriveFile(
                        id=f"{drive_id}/{it['id']}",
                        name=it.get("name", it["id"]),
                        mime_type=(it.get("file") or {}).get("mimeType", ""),
                        # cTag は内容変更で変わる（無ければ更新時刻で代替）
                        content_hash=it.get("cTag") or it.get("lastModifiedDateTime", ""),
                    )
                )
            url = body.get("@odata.nextLink") or ""
            params = {}  # nextLink はクエリ込み
        del token
        return files

    def download(self, *, file_id: str, secret: Optional[str]) -> bytes:
        import httpx

        drive_id, item_id = self._split(file_id)
        resp = httpx.get(
            f"{self.API}/drives/{drive_id}/items/{item_id}/content",
            headers={"Authorization": f"Bearer {self._access_token()}"},
            timeout=self._timeout,
            follow_redirects=True,  # /content は 302 でストレージへ誘導される
        )
        resp.raise_for_status()
        return resp.content


class BoxFolderProvider:
    """Box。Client Credentials Grant（enterprise 単位のサービス認証）。

    認可: env の BOX_CLIENT_ID / BOX_CLIENT_SECRET / BOX_ENTERPRISE_ID。
    folder_id は Box のフォルダ ID（数値文字列）。
    """

    API = "https://api.box.com/2.0"

    def __init__(
        self, client_id: str, client_secret: str, enterprise_id: str, *, timeout: float = 30.0
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._enterprise_id = enterprise_id
        self._timeout = timeout
        self._tokens: dict[tuple[str, str], tuple[str, float]] = {}

    def _access_token(self) -> str:
        import time

        import httpx

        key = (self._client_id, self._enterprise_id)
        cached = self._tokens.get(key)
        if cached and time.monotonic() < cached[1] - 60:
            return cached[0]
        resp = httpx.post(
            "https://api.box.com/oauth2/token",
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "grant_type": "client_credentials",
                "box_subject_type": "enterprise",
                "box_subject_id": self._enterprise_id,
            },
            timeout=self._timeout,
        )
        resp.raise_for_status()
        body = resp.json()
        token = str(body["access_token"])
        self._tokens[key] = (token, time.monotonic() + float(body.get("expires_in", 3600)))
        return token

    def list_files(self, *, folder_id: str, secret: Optional[str]) -> list[DriveFile]:
        import httpx

        files: list[DriveFile] = []
        offset = 0
        while True:
            resp = httpx.get(
                f"{self.API}/folders/{folder_id}/items",
                params={
                    "fields": "id,name,type,sha1",
                    "limit": "100",
                    "offset": str(offset),
                },
                headers={"Authorization": f"Bearer {self._access_token()}"},
                timeout=self._timeout,
            )
            resp.raise_for_status()
            body = resp.json()
            entries = body.get("entries", [])
            for it in entries:
                if it.get("type") != "file":
                    continue
                files.append(
                    DriveFile(
                        id=str(it["id"]),
                        name=it.get("name", str(it["id"])),
                        mime_type="",
                        content_hash=it.get("sha1") or "",
                    )
                )
            offset += len(entries)
            if offset >= int(body.get("total_count", 0)) or not entries:
                return files

    def download(self, *, file_id: str, secret: Optional[str]) -> bytes:
        import httpx

        resp = httpx.get(
            f"{self.API}/files/{file_id}/content",
            headers={"Authorization": f"Bearer {self._access_token()}"},
            timeout=self._timeout,
            follow_redirects=True,  # /content は 302 でストレージへ誘導される
        )
        resp.raise_for_status()
        return resp.content
