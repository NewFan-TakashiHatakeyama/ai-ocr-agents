# mypy: ignore-errors
"""export-svc 常駐エントリ（`python -m newfan_export.worker_main`, §2.1 / §9）。

env から本番アダプタを配線し q.export を消費し続ける ECS タスク本体。SIGTERM で graceful。

必須 env: DATABASE_URL, REDIS_URL
任意 env: EXPORT_S3_BUCKET（未設定は EXPORT_STORAGE_ROOT へローカル保存）, EXPORT_S3_KMS_KEY_ID,
          EXPORT_STORAGE_ROOT（既定 ./data/export）, CONSUMER_NAME
"""

from __future__ import annotations

import os
import signal
import socket
from pathlib import Path
from typing import Any

from newfan_export.pg_source import PgExportSource
from newfan_export.redis_io import RedisStreamConsumer
from newfan_export.service import ExportService
from newfan_export.webhook import WebhookSender
from newfan_export.worker import ExportWorker

_STOP = False


def _handle_sigterm(*_: Any) -> None:
    global _STOP
    _STOP = True


def _store():
    bucket = os.environ.get("EXPORT_S3_BUCKET")
    if bucket:
        from newfan_export.storage import S3ObjectStore

        return S3ObjectStore(bucket, kms_key_id=os.environ.get("EXPORT_S3_KMS_KEY_ID"))
    from newfan_export.storage import LocalObjectStore

    return LocalObjectStore(Path(os.environ.get("EXPORT_STORAGE_ROOT", "./data/export")))


def main() -> None:
    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)

    # webhook の署名鍵は secret_ref（Secrets Manager）参照が正。移行期間の旧行のみ
    # config.secret に fallback する（§16.5 / P6）。
    def _resolve_secret(secret_ref: str, _cache: dict = {}) -> str:  # noqa: B006 - プロセス内キャッシュ
        if secret_ref not in _cache:
            import boto3

            _cache[secret_ref] = boto3.client("secretsmanager").get_secret_value(
                SecretId=secret_ref
            )["SecretString"]
        return _cache[secret_ref]

    source = PgExportSource(os.environ["DATABASE_URL"], resolve_secret=_resolve_secret)
    service = ExportService(_store(), WebhookSender())
    consumer = RedisStreamConsumer(
        os.environ["REDIS_URL"],
        "q.export",
        "export",
        os.environ.get("CONSUMER_NAME", socket.gethostname()),
    )
    worker = ExportWorker(source, service, consumer)
    print("[export] export-svc 起動: q.export を消費します")
    while not _STOP:
        worker.run_once()
    print("[export] SIGTERM 受信: 停止しました")


if __name__ == "__main__":
    main()
