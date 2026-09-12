"""接続の疎通テスト（webhook / s3, §16.5）。

POST /connections/{id}/test の type 別の実体。routers はここを呼び、失敗
（ConnectionTestError）を ApiError に変換する。

方針:
- webhook は**本配信と同じ経路**で試す。SSRF ガード（newfan_netguard.is_blocked_url）を
  通し、本文の符号化と署名ヘッダも本配信（newfan_export.webhook.WebhookSender）と
  同じ関数（newfan_netguard.encode_event / signed_headers）で作る。受信側が署名検証を
  テストで通せば本配信も通る（逆も同じ）
- s3 は sink（orchestrator の S3FileWriter）と同じ作り方のクライアントで HeadBucket。
  バケットの実在と、タスクロールに s3:ListBucket があることが分かる（PutObject 権限は
  書いてみるまで分からないが、バケット名の typo と権限ゼロは登録直後に潰せる）
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
    """sink（S3FileWriter）と同じ作り方。認証はタスクロール / 環境変数に任せる。"""
    import boto3  # 遅延 import（runtime extra）

    return boto3.client("s3")


def build_test_event(connection_id: str, *, tenant_id: str, occurred_at: str) -> dict[str, Any]:
    """疎通テストの本文。受信側が本配信と区別できるよう event='test' を名乗る。"""
    return {
        "event": "test",
        "connection_id": connection_id,
        "tenant_id": tenant_id,
        "occurred_at": occurred_at,
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
