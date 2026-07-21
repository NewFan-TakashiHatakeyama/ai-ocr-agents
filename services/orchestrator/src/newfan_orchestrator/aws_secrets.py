# mypy: ignore-errors
"""Secrets Manager の secret_ref 解決（§16.5 / P6）。

connections は secret_ref（ARN または名前）だけを DB に持ち、秘密の実体はここで
実行時に引く。task ロールの権限は ai-ocr/<env>/conn/* に限定（terraform main.tf）。
プロセス内キャッシュを持つ（sink/配信のたびに GetSecretValue を叩かない。
ローテーション反映はプロセス再起動＝デプロイ単位で十分）。
"""

from __future__ import annotations

import threading


class SecretsManagerResolver:
    def __init__(self) -> None:
        self._client = None
        self._cache: dict[str, str] = {}
        self._lock = threading.Lock()

    def __call__(self, secret_ref: str) -> str:
        with self._lock:
            if secret_ref in self._cache:
                return self._cache[secret_ref]
        import boto3

        if self._client is None:
            self._client = boto3.client("secretsmanager")
        value = self._client.get_secret_value(SecretId=secret_ref)["SecretString"]
        with self._lock:
            self._cache[secret_ref] = value
        return value
