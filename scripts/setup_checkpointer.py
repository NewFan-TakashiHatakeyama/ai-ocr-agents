"""langgraph チェックポイント表を作る（§4.4）。

PostgresSaver.setup() はチェックポイント表の DDL を流す。これはスキーマ変更なので
migrate の仕事。ワーカー側で呼んでいたが、§7.3 でアプリを所有者でないロールへ移した結果
schema public への CREATE 権限が無く、全ジョブが permission denied で落ちた（実 AWS で検出）。

所有者で実行するため、ALTER DEFAULT PRIVILEGES（ensure_app_role.py が設定）により
アプリロールへの GRANT は自動で付く。念のため明示的にも GRANT する
（表が先に作られていた場合、default privileges は遡って適用されないため）。
"""

from __future__ import annotations

import os
from urllib.parse import unquote, urlparse

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import sql


def main() -> int:
    dsn = os.environ["DATABASE_URL"].replace("+psycopg", "")
    with PostgresSaver.from_conn_string(dsn) as saver:
        saver.setup()
    print("[checkpointer] チェックポイント表を用意しました")

    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        print("[checkpointer] APP_DATABASE_URL が無いので GRANT はスキップします")
        return 0
    user = unquote(urlparse(app_url.replace("+psycopg", "")).username or "")
    if not user:
        return 0

    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public'"
            " AND (tablename LIKE 'checkpoint%' OR tablename = 'checkpoint_migrations')"
        )
        tables = [r[0] for r in cur.fetchall()]
        for t in tables:
            cur.execute(
                sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON {} TO {}").format(
                    sql.Identifier(t), sql.Identifier(user)
                )
            )
        print(f"[checkpointer] {user} に GRANT: {tables}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
