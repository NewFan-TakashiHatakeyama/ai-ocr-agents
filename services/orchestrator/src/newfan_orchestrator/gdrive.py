# mypy: ignore-errors
"""Google Drive プロバイダ（⑤⑥ SaaS連携）。

方針（ユーザー決定 2026-08-17）: モックファーストで全配線を実装・検証し、
実クレデンシャルは .env 差し替えだけで有効化できる設計にする。

- FakeGDriveProvider: ローカルディレクトリを Drive フォルダに見立てる開発用。
  root/<folder_id>/ 配下のファイル = Drive 内のファイル。compose でディレクトリを
  マウントすれば「ファイルを置く → 検知 → 取込 → ワークフロー発火」の
  E2E を実 API なしで再現できる。
- GoogleDriveProvider: 実 Drive API v3（httpx 直叩き・SDK 非依存）。
  client_id/secret は env（GOOGLE_OAUTH_CLIENT_ID/SECRET）、リフレッシュトークンは
  接続の secret_ref から解決する。モックファースト方針のため実アカウントでの
  E2E は未了 — 有効化は env が揃った時のみ（worker_main の配線側で選択）。
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Optional, Protocol


@dataclass(frozen=True)
class DriveFile:
    """フォルダ内の 1 ファイル。content_hash は冪等 claim（DD-13）のキーに使う。"""

    id: str
    name: str
    mime_type: str
    content_hash: str


class GDriveProvider(Protocol):
    def list_files(self, *, folder_id: str, secret: Optional[str]) -> list[DriveFile]: ...
    def download(self, *, file_id: str, secret: Optional[str]) -> bytes: ...


class FakeGDriveProvider:
    """開発用。root/<folder_id>/ 直下のファイルを Drive フォルダとして返す。

    file_id は "<folder_id>/<ファイル名>"（フォルダ内で一意・download で曖昧なく解決）。
    content_hash は内容の sha256 先頭16。同名ファイルを別内容で置き直すと hash が
    変わり再取込される（Drive の「同じファイルの更新」と同じ振る舞い）。
    """

    def __init__(self, root: str) -> None:
        self._root = root

    def _dir(self, folder_id: str) -> str:
        # folder_id をパス区切りで汚染されないよう basename 化（../ 対策）
        return os.path.join(self._root, os.path.basename(folder_id))

    def list_files(self, *, folder_id: str, secret: Optional[str]) -> list[DriveFile]:
        d = self._dir(folder_id)
        if not os.path.isdir(d):
            # 空フォルダ（存在する）は正常だが、不存在は「疎通NG」。[] を返すと
            # folder_id のタイポでも tested に昇格し、検知ゼロのまま「テスト済」と
            # 表示されるサイレント故障になる（レビュー確定）
            raise FileNotFoundError(f"gdrive fake folder not found: {folder_id}")
        folder = os.path.basename(folder_id)
        out: list[DriveFile] = []
        for name in sorted(os.listdir(d)):
            path = os.path.join(d, name)
            if not os.path.isfile(path):
                continue
            with open(path, "rb") as f:
                digest = hashlib.sha256(f.read()).hexdigest()[:16]
            out.append(
                DriveFile(id=f"{folder}/{name}", name=name, mime_type="", content_hash=digest)
            )
        return out

    def download(self, *, file_id: str, secret: Optional[str]) -> bytes:
        folder, _, name = file_id.partition("/")
        # list_files が返した "<folder>/<name>" 以外（パス走査等）を読ませない
        path = os.path.join(self._root, os.path.basename(folder), os.path.basename(name))
        if not name or not os.path.isfile(path):
            raise FileNotFoundError(file_id)
        with open(path, "rb") as f:
            return f.read()


class GoogleDriveProvider:
    """実 Drive API v3。httpx 直叩き（googleapis SDK 非依存・依存を増やさない）。

    認可: OAuth リフレッシュトークン（接続の secret_ref で解決した値）を
    client_id/secret（env）でアクセストークンに交換する。トークンは
    プロセス内で有効期限までキャッシュする。

    注意: モックファースト方針（2026-08-17 ユーザー決定）のため、実アカウントでの
    E2E 検証は未了。有効化は GOOGLE_OAUTH_CLIENT_ID/SECRET が揃った時のみ。
    """

    TOKEN_URL = "https://oauth2.googleapis.com/token"
    API = "https://www.googleapis.com/drive/v3"

    def __init__(self, client_id: str, client_secret: str, *, timeout: float = 30.0) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._timeout = timeout
        # refresh_token ごとにキャッシュする。単一インスタンスを全テナント・全接続で
        # 共有するため、鍵なしのキャッシュだと別アカウントのアクセストークンを混用し、
        # 他テナントの資格情報で Drive を読む（認可境界・監査帰属の破れ。レビュー確定major）
        self._tokens: dict[str, tuple[str, float]] = {}

    def _access_token(self, refresh_token: str) -> str:
        import time

        import httpx

        cached = self._tokens.get(refresh_token)
        if cached and time.monotonic() < cached[1] - 60:
            return cached[0]
        resp = httpx.post(
            self.TOKEN_URL,
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=self._timeout,
        )
        resp.raise_for_status()
        body = resp.json()
        token = str(body["access_token"])
        self._tokens[refresh_token] = (
            token,
            time.monotonic() + float(body.get("expires_in", 3600)),
        )
        return token

    def list_files(self, *, folder_id: str, secret: Optional[str]) -> list[DriveFile]:
        import httpx

        if not secret:
            raise RuntimeError("gdrive 接続に secret_ref（リフレッシュトークン）がありません")
        token = self._access_token(secret)
        files: list[DriveFile] = []
        page_token: Optional[str] = None
        while True:
            params = {
                "q": f"'{folder_id}' in parents and trashed=false",
                "fields": "nextPageToken, files(id, name, mimeType, md5Checksum, modifiedTime)",
                "pageSize": "100",
            }
            if page_token:
                params["pageToken"] = page_token
            resp = httpx.get(
                f"{self.API}/files",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
                timeout=self._timeout,
            )
            resp.raise_for_status()
            body = resp.json()
            for f in body.get("files", []):
                files.append(
                    DriveFile(
                        id=f["id"],
                        name=f.get("name", f["id"]),
                        mime_type=f.get("mimeType", ""),
                        # Google Docs 形式は md5 が無い → modifiedTime で変更検知
                        content_hash=f.get("md5Checksum") or f.get("modifiedTime", ""),
                    )
                )
            page_token = body.get("nextPageToken")
            if not page_token:
                return files

    def download(self, *, file_id: str, secret: Optional[str]) -> bytes:
        import httpx

        if not secret:
            raise RuntimeError("gdrive 接続に secret_ref（リフレッシュトークン）がありません")
        token = self._access_token(secret)
        resp = httpx.get(
            f"{self.API}/files/{file_id}",
            params={"alt": "media"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.content
