"""アプリ用の非所有ロールを作る／更新する（§7.3 RLS）。

PostgreSQL の**テーブル所有者は RLS をバイパスする**。アプリがマスタユーザ（=所有者）で
繋いでいる限り、FORCE を付けようとポリシーを書こうとテナント分離は効かない。
所有者は migrate/バッチ専用に残し、アプリは所有者でないロールで繋ぐ。

terraform から RDS の中にロールは作れないため、接続情報だけ terraform が作り
（Secrets Manager の app-database-url）、ロールの実体はここで作る。
migrate タスクのコンテナ内で、所有者の DATABASE_URL を使って実行する。

  DATABASE_URL=<所有者>       ... 実行に使う接続
  APP_DATABASE_URL=<アプリ用>  ... ここからユーザ名/パスワードを取り出して作る

冪等。既にあればパスワードと権限を更新するだけ。
"""

from __future__ import annotations

import os
import sys
from urllib.parse import unquote, urlparse

import psycopg
from psycopg import sql

# アプリが触る業務テーブル。RLS のポリシーで tenant_id 一致行だけが見える。
# GRANT は「public の全テーブルへ一括」+「alembic_version だけ剥奪」で管理する。
# 以前はテーブルの明示リストを保持していたが、migration 0003 の workflow_node_runs を
# 足し忘れて本番のワークフロー実行が全滅した（敵対的レビュー確定）。明示リストは
# 一括 GRANT の導入後は「消しても権限が剥がれない死んだ許可リスト」になるため撤去した。
# 権限を絞りたいテーブルは下の REVOKE 節に足すこと（それが唯一の絞り口）。

def main() -> int:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        print("[role] APP_DATABASE_URL が無いのでスキップします（ローカル/単一ロール構成）")
        return 0

    parsed = urlparse(app_url.replace("+psycopg", ""))
    user = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    if not user or not password:
        print("[role] APP_DATABASE_URL からユーザ/パスワードを取り出せません", file=sys.stderr)
        return 2

    dsn = os.environ["DATABASE_URL"].replace("+psycopg", "")
    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database()")
            dbname = cur.fetchone()[0]  # type: ignore[index]

            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (user,))
            exists = cur.fetchone() is not None
            ident = sql.Identifier(user)
            if exists:
                # パスワードは terraform が毎回生成し直すので必ず追随させる
                cur.execute(sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(
                    ident, sql.Literal(password)))
                print(f"[role] {user} を更新しました")
            else:
                cur.execute(sql.SQL("CREATE ROLE {} WITH LOGIN PASSWORD {}").format(
                    ident, sql.Literal(password)))
                print(f"[role] {user} を作成しました")

            # BYPASSRLS を持っていたら剥がす。持っていると RLS が丸ごと無効になる。
            # NOSUPERUSER は付けない。RDS のマスタは superuser ではないため
            # 「Only roles with the SUPERUSER attribute may change the SUPERUSER attribute」
            # で弾かれる（実測）。新規ロールは既定で非 superuser なので実害はなく、
            # 万一 superuser だった場合は最後の検査で気づける。
            cur.execute(sql.SQL("ALTER ROLE {} NOBYPASSRLS").format(ident))

            cur.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(dbname), ident))
            cur.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(ident))
            # 既存テーブルへ一括 GRANT（ALTER DEFAULT PRIVILEGES は「これから作られる表」に
            # しか効かない）。明示リスト方式は 0003 の workflow_node_runs 足し忘れで
            # 本番のワークフロー実行が全滅したため廃止（レビュー確定）
            cur.execute(sql.SQL(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {}"
            ).format(ident))
            # alembic_version はアプリが触る理由が無い。書込権限があると、アプリ接続を
            # 奪われた際に migration 版数の改竄（無音のダウングレード）が可能になる
            # （レビュー確定）。SELECT も不要なので全剥奪
            cur.execute(sql.SQL(
                "REVOKE ALL ON alembic_version FROM {}"
            ).format(ident))
            cur.execute(sql.SQL(
                "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}").format(ident))
            # 今後 migration が足すテーブルにも自動で GRANT されるようにする
            # （新テーブル追加のたびに本スクリプトの更新を要さないため）。
            cur.execute(sql.SQL(
                "ALTER DEFAULT PRIVILEGES IN SCHEMA public"
                " GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}").format(ident))
            cur.execute(sql.SQL(
                "ALTER DEFAULT PRIVILEGES IN SCHEMA public"
                " GRANT USAGE, SELECT ON SEQUENCES TO {}").format(ident))

            # §11: audit_logs は append-only。業務ロールから UPDATE/DELETE を剥奪する。
            cur.execute(sql.SQL("REVOKE UPDATE, DELETE ON audit_logs FROM {}").format(ident))

            cur.execute("SELECT rolbypassrls, rolsuper FROM pg_roles WHERE rolname = %s", (user,))
            bypass, is_super = cur.fetchone()  # type: ignore[misc]
            print(f"[role] {user}: rolbypassrls={bypass} rolsuper={is_super}")
            if bypass or is_super:
                print("[role] ★このロールは RLS をバイパスします。分離が効きません。", file=sys.stderr)
                return 1
    print("[role] 完了")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
