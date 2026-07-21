"""RDS で BYPASSRLS 付きロールを作れるかを測る（§7.3 の実現方式を決めるため）。

PostgreSQL の BYPASSRLS 属性は superuser でないと付けられない。RDS のマスターユーザは
superuser ではなく rds_superuser のメンバなので、付けられるかは実際に試さないと分からない。

  付けられる → FORCE RLS + 非所有ロール + migrate に BYPASSRLS（多層）
  付けられない → 非所有ロール（所有者=migrate は既定でバイパス）で設計意図を満たす

一時ロールを作って消すだけ。既存データは触らない。
"""

from __future__ import annotations

import os

import psycopg


def main() -> int:
    dsn = os.environ["DATABASE_URL"].replace("+psycopg", "")
    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT current_user")
            print("接続ロール:", cur.fetchone()[0])  # type: ignore[index]

            cur.execute("SELECT rolname FROM pg_roles WHERE pg_has_role(current_user, oid, 'member')")
            print("所属ロール:", sorted(r[0] for r in cur.fetchall()))

            for attr in ("BYPASSRLS", "CREATEROLE"):
                name = f"probe_{attr.lower()}"
                try:
                    cur.execute(f"CREATE ROLE {name} NOLOGIN {attr}")
                    cur.execute(
                        "SELECT rolbypassrls FROM pg_roles WHERE rolname = %s", (name,)
                    )
                    got = cur.fetchone()[0]  # type: ignore[index]
                    print(f"  CREATE ROLE ... {attr}: OK (rolbypassrls={got})")
                    cur.execute(f"DROP ROLE {name}")
                except Exception as exc:  # noqa: BLE001 - 何が拒否されたかを見たい
                    print(f"  CREATE ROLE ... {attr}: DENIED -> {str(exc).splitlines()[0][:100]}")
                    try:
                        cur.execute(f"DROP ROLE IF EXISTS {name}")
                    except Exception:  # noqa: BLE001, S110
                        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
