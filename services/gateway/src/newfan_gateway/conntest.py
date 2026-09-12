"""接続の疎通テスト（webhook / s3, §16.5）。

POST /connections/{id}/test の type 別の実体。routers はここを呼び、失敗
（ConnectionTestError）を ApiError に変換する。

方針:
- webhook は**本配信と同じ経路**で試す。SSRF ガード（newfan_netguard.is_blocked_url）を
  通し、本文の符号化と署名ヘッダも本配信（newfan_export.webhook.WebhookSender）と
  同じ関数（newfan_netguard.encode_event / signed_headers）で作る。受信側が署名検証を
  テストで通せば本配信も通る（逆も同じ）
- type=webhook の接続は sink.webhook（署名付き JSON イベント）だけでなく sink.notify
  （Slack incoming webhook 互換。orchestrator の NotifySender が {"text": …} を送る）
  にも使われる。Slack は text / blocks / attachments の無い本文を 400（no_text）で
  断るため、test イベントには **text も含める**。HMAC 検証する受信側は余分なキーを
  無視し（署名は本文全体に掛かる）、Slack 互換の通知先にはテスト投稿として届く。
  どちらの受信側でも 2xx が返り、tested に上げられる（L010 が解ける）
- s3 は sink（orchestrator の S3FileWriter）と同じ `boto3.client("s3")` で HeadBucket。
  ただし同期 API の中で待つので、タイムアウトと再試行は短く固定する（botocore の既定は
  接続 60 秒・読取 60 秒・最大 5 回で、到達不能なら 1 クリックが数分スレッドを掴む）。
  バケットの実在と、タスクロールに s3:ListBucket があることが分かる（PutObject 権限は
  書いてみるまで分からないが、バケット名の typo と権限ゼロは登録直後に潰せる）
- URL の妥当性は httpx の厳格なパーサでも見る（is_blocked_url の urllib.parse は
  ポートが数字でない・制御文字入りの URL も通す。httpx.InvalidURL は HTTPError の
  派生ではないので、拾わないと 500 になる）。登録時（POST /connections）と送信前の
  両方で同じ関数（invalid_url_reason）を使う
- 失敗理由は利用者向けの文言だけを返す。内部例外の文字列（DSN・トークン・スタック等の
  断片が混ざり得る）はそのまま外に出さない
"""

from __future__ import annotations

from typing import Any, Optional

import httpx
from newfan_netguard import (
    Resolver,
    default_resolver,
    encode_event,
    is_blocked_url,
    signed_headers,
)

# 疎通テストは同期 API の中で待つので短めに（本配信は 10 秒）
WEBHOOK_TIMEOUT_SEC = 5.0
# S3 も同じ理由で短く（接続・読取とも 5 秒、再試行なし = 合計 1 回）。sink 側は
# worker で動くので botocore の既定（60 秒 × 最大 5 回）で構わないが、ここは
# スレッドプールの worker を掴んだまま待つ
S3_TIMEOUT_SEC = 5


class ConnectionTestError(Exception):
    """疎通テストの失敗。message は利用者向け（内部例外の文言を含めない）。"""

    def __init__(self, message: str, *, details: Optional[dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


def new_http_client(timeout: float) -> httpx.Client:
    """テストが MockTransport に差し替える継ぎ目（httpx.Client 自体は TestClient も使う）。"""
    return httpx.Client(timeout=timeout)


def new_s3_client() -> Any:
    """sink（S3FileWriter）と同じ boto3.client("s3")（認証はタスクロール / 環境変数）。

    ただしタイムアウトと再試行は同期 API 向けに短く固定する（S3_TIMEOUT_SEC）。
    """
    import boto3  # 遅延 import（runtime extra）
    from botocore.config import Config

    return boto3.client(
        "s3",
        config=Config(
            connect_timeout=S3_TIMEOUT_SEC,
            read_timeout=S3_TIMEOUT_SEC,
            retries={"total_max_attempts": 1},
        ),
    )


def invalid_url_reason(url: str) -> Optional[str]:
    """httpx が受け付けない URL なら利用者向けの理由、受け付けるなら None。

    is_blocked_url（urllib.parse 由来）は「:abc」のようなポートや改行入りの URL も
    通してしまう。httpx.InvalidURL は HTTPError の派生ではないため、送信で初めて
    落ちると 500（内部エラー）になる。登録時と送信前の両方でここを通す。
    """
    try:
        httpx.URL(url)
    except httpx.InvalidURL:
        return "配信先 URL が不正です（ポートは数字、改行や制御文字は不可）"
    return None


def build_test_event(connection_id: str, *, tenant_id: str, occurred_at: str) -> dict[str, Any]:
    """疎通テストの本文。受信側が本配信と区別できるよう event='test' を名乗る。

    text は Slack incoming webhook 互換の通知先（sink.notify）向け。無いと Slack が
    400（no_text）を返し、その URL は永久に tested になれない。署名検証する受信側
    （sink.webhook）は本文全体の HMAC を見るだけなので、余分なキーがあっても通る。
    """
    return {
        "event": "test",
        "connection_id": connection_id,
        "tenant_id": tenant_id,
        "occurred_at": occurred_at,
        "text": f"[NewFan AI-OCR] 接続テスト: この通知先への疎通を確認しました（{connection_id}）",
    }


def check_webhook(
    url: str,
    secret: str,
    event: dict[str, Any],
    *,
    resolver: Resolver = default_resolver,
    timeout: float = WEBHOOK_TIMEOUT_SEC,
) -> int:
    """署名付きの test イベントを 1 回送る。2xx なら status code を返し、それ以外は例外。"""
    if is_blocked_url(url, resolver=resolver):
        raise ConnectionTestError(
            "配信先 URL が拒否されました（http(s) 以外・内部ネットワーク宛て・名前解決不能）",
            details={"url": url},
        )
    bad = invalid_url_reason(url)
    if bad is not None:
        raise ConnectionTestError(bad, details={"url": url})
    body = encode_event(event)
    headers = signed_headers(body, secret)
    try:
        with new_http_client(timeout) as client:
            resp = client.post(url, content=body, headers=headers)
    except httpx.TimeoutException as exc:
        raise ConnectionTestError(
            f"配信先が {timeout:g} 秒以内に応答しませんでした", details={"url": url}
        ) from exc
    except httpx.ConnectError as exc:
        raise ConnectionTestError(
            "配信先に接続できません（名前解決・接続拒否・TLS のいずれか）", details={"url": url}
        ) from exc
    except httpx.InvalidURL as exc:
        # invalid_url_reason で弾けなかった形（httpx の版差）も 500 にはしない
        raise ConnectionTestError(
            "配信先 URL が不正です（ポートは数字、改行や制御文字は不可）", details={"url": url}
        ) from exc
    except httpx.HTTPError as exc:
        # 例外文言には URL 以外（プロキシ設定等）の断片が混ざり得るため型名だけ返す
        raise ConnectionTestError(
            f"送信に失敗しました（{type(exc).__name__}）", details={"url": url}
        ) from exc
    if not 200 <= resp.status_code < 300:
        raise ConnectionTestError(
            f"配信先が HTTP {resp.status_code} を返しました（2xx が必要です）",
            details={"url": url, "status_code": resp.status_code},
        )
    return resp.status_code


def check_s3(bucket: str) -> None:
    """HeadBucket。存在しない・権限が無い・認証情報が無いを区別して返す。"""
    try:
        from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
    except ModuleNotFoundError as exc:  # pragma: no cover - runtime extra 未導入
        raise ConnectionTestError("S3 クライアント（boto3）が入っていません") from exc

    try:
        client = new_s3_client()
        client.head_bucket(Bucket=bucket)
    except NoCredentialsError as exc:
        raise ConnectionTestError(
            "AWS の認証情報がありません（タスクロール / AWS_ACCESS_KEY_ID）",
            details={"bucket": bucket},
        ) from exc
    except ClientError as exc:
        err = exc.response.get("Error") or {}
        code = str(err.get("Code") or "")
        status = (exc.response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
        if code in ("404", "NoSuchBucket") or status == 404:
            msg = f"バケットが存在しません: {bucket}"
        elif code in ("403", "AccessDenied", "Forbidden") or status == 403:
            msg = f"バケットへのアクセス権がありません（s3:ListBucket）: {bucket}"
        elif code in ("301", "PermanentRedirect") or status == 301:
            msg = f"バケットのリージョンが実行環境の既定と異なります: {bucket}"
        else:
            msg = f"S3 がエラーを返しました（{code or status or 'unknown'}）"
        raise ConnectionTestError(
            msg, details={"bucket": bucket, "code": code, "http_status": status}
        ) from exc
    except BotoCoreError as exc:
        raise ConnectionTestError(
            f"S3 に接続できません（{type(exc).__name__}）", details={"bucket": bucket}
        ) from exc
