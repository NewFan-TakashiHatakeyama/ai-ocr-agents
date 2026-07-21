# mypy: ignore-errors
"""sink アダプタ（§16 設計 v0.2 §9 / P6）。

SQL は newfan_workflow.dbsink（dry-run と同一実装）が生成済みのものを受け取り、
ここでは**実行だけ**を行う。全行を 1 トランザクションで書く（部分書込みを残さない。
クラッシュ時は全 rollback → 再実行は台帳キー/upsert の冪等で二重にならない）。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class PgDbWriter:
    """顧客 DB（PostgreSQL）へのプリペアド書込み。

    接続は都度張って閉じる（sink は run の終端で 1 回だけ。プールを持つほど
    頻度が無く、顧客 DB へ常時接続を残さない方が行儀が良い）。
    """

    def __init__(self, *, connect_timeout: float = 10.0) -> None:
        self._timeout = connect_timeout

    def write(self, dsn: str, sql: str, rows: list[dict[str, Any]]) -> int:
        import psycopg

        with psycopg.connect(dsn, connect_timeout=self._timeout) as conn:
            with conn.cursor() as cur:
                cur.executemany(sql, rows)
                affected = cur.rowcount
            conn.commit()
        # executemany の rowcount は ON CONFLICT DO NOTHING で捨てた行を含まない
        # ドライバもある。負値（不明）は 0 に丸める
        return max(affected, 0)


class S3FileWriter:
    """sink.file の書込み先（type='s3' 接続のバケット）。同一キー上書き＝冪等。"""

    def __init__(self, *, kms_key_id: str | None = None) -> None:
        self._kms = kms_key_id
        self._client = None

    def write(self, bucket: str, key: str, body: bytes, content_type: str) -> None:
        import boto3

        if self._client is None:
            self._client = boto3.client("s3")
        extra: dict[str, Any] = {"ContentType": content_type}
        if self._kms:
            extra.update(ServerSideEncryption="aws:kms", SSEKMSKeyId=self._kms)
        self._client.put_object(Bucket=bucket, Key=key, Body=body, **extra)


class NotifySender:
    """sink.notify（Slack incoming webhook 互換）。text を JSON で POST する。

    URL は connections(type='webhook') から来る。SSRF ガードは webhook 配信と同じ
    newfan_netguard を通す（内部ネットワークへ送らせない）。署名は付けない
    （Slack incoming webhook の契約に無く、URL 自体が秘密）。
    """

    def __init__(self, *, timeout: float = 10.0) -> None:
        self._timeout = timeout

    def send(self, url: str, text: str) -> bool:
        import httpx
        from newfan_netguard import is_blocked_url

        if is_blocked_url(url):
            logger.warning("[notify] 宛先が SSRF ガードで拒否されました: %s", url)
            return False
        try:
            resp = httpx.post(url, json={"text": text}, timeout=self._timeout)
            return resp.status_code < 300
        except httpx.HTTPError:
            logger.warning("[notify] 送信に失敗しました", exc_info=True)
            return False
