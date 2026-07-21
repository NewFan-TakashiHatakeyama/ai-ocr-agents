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


# ワークフロー層（§16）の checkpointer 用スキーマ。PostgresSaver はテーブル名が
# 固定・スキーマ非修飾で Python 版に schema= 引数が無いため（Issue #7345）、
# 接続文字列の search_path で分離する。抽出グラフ（public）と混ざると DD-11 を破る。
# 分離が効くことはローカル実測済み（scripts/probe_langgraph_workflow.py）。
LG_WF_SCHEMA = "lg_wf"


def _wf_dsn(dsn: str) -> str:
    sep = "&" if "?" in dsn else "?"
    return f"{dsn}{sep}options=-csearch_path%3D{LG_WF_SCHEMA}"


def main() -> int:
    dsn = os.environ["DATABASE_URL"].replace("+psycopg", "")
    with PostgresSaver.from_conn_string(dsn) as saver:
        saver.setup()
    print("[checkpointer] チェックポイント表を用意しました")

    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {LG_WF_SCHEMA}")
    with PostgresSaver.from_conn_string(_wf_dsn(dsn)) as saver:
        saver.setup()
    print(f"[checkpointer] ワークフロー用（{LG_WF_SCHEMA} スキーマ）を用意しました")

    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        print("[checkpointer] APP_DATABASE_URL が無いので GRANT はスキップします")
        return 0
    user = unquote(urlparse(app_url.replace("+psycopg", "")).username or "")
    if not user:
        return 0

    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        for schema in ("public", LG_WF_SCHEMA):
            cur.execute(
                sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                    sql.Identifier(schema), sql.Identifier(user)
                )
            )
            cur.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = %s"
                " AND tablename LIKE 'checkpoint%%'",
                (schema,),
            )
            tables = [r[0] for r in cur.fetchall()]
            for t in tables:
                cur.execute(
                    sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON {}.{} TO {}").format(
                        sql.Identifier(schema), sql.Identifier(t), sql.Identifier(user)
                    )
                )
            print(f"[checkpointer] {user} に GRANT: {schema}.{tables}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
