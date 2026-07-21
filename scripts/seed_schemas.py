"""field_schemas を投入する（ゴールデンセット計測 / dev の前提）。

RDS はプライベート VPC にいるため手元からは繋がらない。migrate タスク定義の
コンテナ内で実行する前提で、`scripts/aws_env.sh seed-schemas` から呼ばれる。

  DATABASE_URL=... TENANT_ID=ten_1 python seed_schemas.py '<schemas.json の中身>'

引数を省くと標準入力から読む。
"""

from __future__ import annotations

import json
import os
import sys

import psycopg


def main() -> int:
    raw = sys.argv[1] if len(sys.argv) > 1 else sys.stdin.read()
    doc = json.loads(raw)
    tenant = os.environ.get("TENANT_ID", "ten_1")
    dsn = os.environ["DATABASE_URL"].replace("+psycopg", "")

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            for s in doc["schemas"]:
                # 同じ doc_type の旧版が is_active のままだと、どちらが引かれるか
                # 実行時まで分からない（§4.6.1 は doc_type で 1 本引く）。先に降ろす。
                cur.execute(
                    "UPDATE field_schemas SET is_active = false"
                    " WHERE tenant_id = %s AND doc_type = %s",
                    (tenant, s["doc_type"]),
                )
                cur.execute(
                    """
                    INSERT INTO field_schemas
                        (id, tenant_id, doc_type, version, fields, is_active, created_by)
                    VALUES (%s, %s, %s, %s, %s, true, 'golden-seed')
                    ON CONFLICT (id) DO UPDATE SET
                        fields = EXCLUDED.fields,
                        version = EXCLUDED.version,
                        is_active = true
                    """,
                    (
                        s["id"],
                        tenant,
                        s["doc_type"],
                        s["version"],
                        # fields 列は「項目定義の配列」そのもの。PgContextStore.load は
                        # {"doc_type": <doc_type列>, "fields": <fields列>} と組み立てるため、
                        # ここで doc_type を包むと二重になって FieldSchema の検証に落ちる。
                        json.dumps(s["fields"]),
                    ),
                )
                print(f"[seed] {s['id']} ({s['doc_type']} v{s['version']}) "
                      f"{len(s['fields'])} 項目")
        conn.commit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
