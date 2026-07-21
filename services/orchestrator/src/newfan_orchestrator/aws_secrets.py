# mypy: ignore-errors
"""secret_ref の解決（§16.5 / P6）。

connections は secret_ref だけを DB に持ち、秘密の実体はここで実行時に引く。
- 既定: AWS Secrets Manager（ARN または名前）。task ロールの権限は
  ai-ocr/<env>/conn/* に限定（terraform main.tf）
- `env:NAME`: 環境変数から読む（**ローカル/compose 用**。AWS の無い結合テストで
  DB に秘密を置かず db_write を動かすための最小の抜け道。本番はコンテナ env に
  秘密を置かない運用なので事実上使われない）

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
        if secret_ref.startswith("env:"):
            import os

            name = secret_ref[4:]
            value = os.environ.get(name)
            if value is None:
                raise KeyError(f"secret_ref の環境変数がありません: {name}")
            return value
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
