"""inbox 用の s3 接続を投入する（§16 P4。migrate タスク内で実行する前提）。

S3 イベント駆動トリガーは connections(type='s3') の config.bucket とイベントの
バケット名を突き合わせて発火する。バケット名はアカウント ID を含み terraform が
決めるため、コードに焼かずここで seed する。

env: DATABASE_URL（所有者）, TENANT_ID, INBOX_BUCKET
"""

from __future__ import annotations

import json
import os

import psycopg

TENANT = os.environ["TENANT_ID"]
BUCKET = os.environ["INBOX_BUCKET"]
CONN_ID = "con_inbox_s3"

with psycopg.connect(os.environ["DATABASE_URL"].replace("+psycopg", "")) as conn:
    conn.execute(
        """
        INSERT INTO connections (id, tenant_id, type, name, config, status)
        VALUES (%s, %s, 's3', 'inbox', %s, 'tested')
        ON CONFLICT (id) DO UPDATE SET config = EXCLUDED.config, status = 'tested'
        """,
        (CONN_ID, TENANT, json.dumps({"bucket": BUCKET})),
    )
    conn.commit()
print(f"s3 connection ok: {CONN_ID} -> {BUCKET} (tenant={TENANT})")
