# mypy: ignore-errors
"""Google Drive 実 OAuth のリフレッシュトークン取得ヘルパー（⑤⑥ 実 E2E 用）。

使い方（リポジトリの venv で実行）:

    .venv/Scripts/python.exe scripts/setup_gdrive_oauth.py \
        --client-id <クライアントID> --client-secret <クライアントシークレット> \
        [--folder <DriveフォルダID>] [--write-env]

やること:
1. ブラウザで Google の同意画面を開く（scope は drive.readonly のみ・読取専用）
2. localhost へのリダイレクトで認可コードを受け取り、リフレッシュトークンに交換
3. --folder 指定時は files.list で疎通を実測（GoogleDriveProvider と同じクエリ）
4. --write-env 指定時は .env に必要な3行＋GDRIVE_FAKE_ROOT=（実APIモード切替）を追記
   （.env は CRLF。既存キーがある場合は追記せず手動更新を案内する）

前提: Google Cloud Console で「デスクトップアプリ」種別の OAuth クライアントを
作成済みであること（手順は docs/gdrive-oauth-setup.md）。
"""

from __future__ import annotations

import argparse
import http.server
import os
import sys
import threading
import urllib.parse
import webbrowser

TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
SCOPE = "https://www.googleapis.com/auth/drive.readonly"


def _receive_code(port: int) -> str:
    """localhost で 1 回だけ認可コードを受け取る。"""
    holder: dict[str, str] = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server の契約
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            if "code" in qs:
                holder["code"] = qs["code"][0]
                self.wfile.write(
                    "<h3>認可を受け取りました。このタブは閉じて構いません。</h3>".encode()
                )
            else:
                holder["error"] = qs.get("error", ["unknown"])[0]
                self.wfile.write(f"<h3>失敗: {holder['error']}</h3>".encode())
            done.set()

        def log_message(self, *args: object) -> None:  # 静かに
            pass

    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        if not done.wait(timeout=300):
            raise TimeoutError("5分以内に同意が完了しませんでした。やり直してください。")
    finally:
        server.shutdown()
    if "error" in holder:
        raise RuntimeError(f"認可が拒否されました: {holder['error']}")
    return holder["code"]


def _append_env(lines: list[str]) -> None:
    """CRLF の .env へ安全に追記する（既存キーは触らない・改行を分離する）。"""
    path = ".env"
    existing = b""
    if os.path.exists(path):
        with open(path, "rb") as f:
            existing = f.read()
    text = existing.decode("utf-8", errors="replace")
    to_add: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0]
        if any(t.split("=", 1)[0].strip() == key for t in text.splitlines()):
            print(f"  .env に {key} は既にあります → 手動で値を更新してください")
            continue
        to_add.append(line)
    if not to_add:
        return
    with open(path, "ab") as f:
        if existing and not existing.endswith((b"\n",)):
            f.write(b"\r\n")  # 改行なし末尾への連結事故を防ぐ（過去に実害あり）
        for line in to_add:
            f.write(line.encode() + b"\r\n")
    print(f"  .env に {len(to_add)} 行を追記しました")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--client-id", default=os.environ.get("GOOGLE_OAUTH_CLIENT_ID"))
    ap.add_argument("--client-secret", default=os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET"))
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--folder", help="疎通確認する Drive フォルダ ID（任意）")
    ap.add_argument("--write-env", action="store_true", help=".env に設定行を追記する")
    args = ap.parse_args()
    if not args.client_id or not args.client_secret:
        ap.error("--client-id / --client-secret（または環境変数）が必要です")

    import httpx

    redirect_uri = f"http://localhost:{args.port}"
    auth = AUTH_URL + "?" + urllib.parse.urlencode(
        {
            "client_id": args.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPE,
            "access_type": "offline",  # リフレッシュトークンを貰う
            "prompt": "consent",  # 再実行でも必ず refresh_token が返るように
        }
    )
    print("ブラウザで Google の同意画面を開きます…")
    print(f"（開かない場合はこの URL を手動で開く）\n  {auth}\n")
    webbrowser.open(auth)
    code = _receive_code(args.port)

    resp = httpx.post(
        TOKEN_URL,
        data={
            "client_id": args.client_id,
            "client_secret": args.client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json()
    refresh = body.get("refresh_token")
    if not refresh:
        print("refresh_token が返りませんでした。同意画面で毎回許可し直しているか確認してください。")
        return 1
    print("✓ リフレッシュトークンを取得しました")

    if args.folder:
        # GoogleDriveProvider.list_files と同じクエリで疎通を実測
        files = httpx.get(
            "https://www.googleapis.com/drive/v3/files",
            params={
                "q": f"'{args.folder}' in parents and trashed=false",
                "fields": "files(id, name)",
                "pageSize": "10",
            },
            headers={"Authorization": f"Bearer {body['access_token']}"},
            timeout=30,
        )
        files.raise_for_status()
        names = [f.get("name") for f in files.json().get("files", [])]
        print(f"✓ フォルダ疎通OK: {len(names)} 件見えます {names[:5]}")

    env_lines = [
        f"GOOGLE_OAUTH_CLIENT_ID={args.client_id}",
        f"GOOGLE_OAUTH_CLIENT_SECRET={args.client_secret}",
        f"GDRIVE_REFRESH_TOKEN={refresh}",
        "GDRIVE_FAKE_ROOT=",  # 空文字 = Fake 無効・実 API モード
    ]
    if args.write_env:
        print(".env へ追記します:")
        _append_env(env_lines)
    else:
        print("\n.env に以下を追記してください（--write-env で自動追記も可）:")
        for line in env_lines:
            key, _, val = line.partition("=")
            shown = val if key in ("GDRIVE_FAKE_ROOT",) else (val[:8] + "…" if val else "")
            print(f"  {key}={shown}")

    print(
        "\n次の手順:\n"
        "  1. docker compose -f deploy/compose.yaml --env-file .env up -d orchestrator-worker\n"
        "     （ログに『gdrive: GoogleDriveProvider 有効』が出ること）\n"
        "  2. 接続管理で GDrive 接続を作成: フォルダID=<実DriveのフォルダID>,\n"
        "     secret_ref=env:GDRIVE_REFRESH_TOKEN → 今すぐ同期 → テスト済に昇格\n"
        "  3. ワークフローを有効化し、Drive のフォルダにファイルを置いて自動取込を確認"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
