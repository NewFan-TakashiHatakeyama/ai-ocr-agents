"""実 RDS で RLS が効いているかを測る（§7.3）。

RLS の効き方は接続ロールの属性で変わる:
  - SUPERUSER / BYPASSRLS 属性  → 無条件にバイパス（FORCE も無視）
  - テーブル所有者            → 既定でバイパス。FORCE ROW LEVEL SECURITY で適用される
  - それ以外                  → 既定で適用される
ローカル compose の newfan は superuser なので AWS の再現にならない。実 RDS で測る。

RDS はプライベート VPC にいるため migrate タスクのコンテナ内で実行する
（scripts/aws_env.sh probe-rls）。読み取りと一時行の作成/削除のみで、既存データは触らない。
"""

from __future__ import annotations

import os

import psycopg

PROBE_A = "ten_probe_a"
PROBE_B = "ten_probe_b"


def main() -> int:
    # APP_DATABASE_URL があればアプリが実際に使う接続で測る。所有者で測っても
    # 「所有者はバイパスする」という当たり前を確認するだけで、アプリの分離は分からない。
    dsn = os.environ.get("APP_DATABASE_URL") or os.environ["DATABASE_URL"]
    owner_dsn = os.environ["DATABASE_URL"].replace("+psycopg", "")
    dsn = dsn.replace("+psycopg", "")

    # プローブ行の作成/削除は所有者で行う（アプリロールは RLS で他テナントを書けない）
    with psycopg.connect(owner_dsn, autocommit=True) as oc, oc.cursor() as ocur:
        for t in (PROBE_A, PROBE_B):
            ocur.execute("INSERT INTO tenants (id, name) VALUES (%s, 'rls probe')"
                         " ON CONFLICT (id) DO NOTHING", (t,))
            ocur.execute(
                "INSERT INTO documents (id, tenant_id, storage_uri, mime_type,"
                " page_count, status) VALUES (%s, %s, 's3://probe', 'image/png', 1,"
                " 'uploaded') ON CONFLICT (id) DO NOTHING",
                (f"doc_{t}", t),
            )

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT current_user, rolsuper, rolbypassrls FROM pg_roles"
                        " WHERE rolname = current_user")
            user, is_super, bypass = cur.fetchone()  # type: ignore[misc]
            cur.execute("SELECT tableowner FROM pg_tables WHERE tablename = 'documents'")
            owner = cur.fetchone()[0]  # type: ignore[index]
            cur.execute("SELECT relrowsecurity, relforcerowsecurity FROM pg_class"
                        " WHERE relname = 'documents'")
            enabled, forced = cur.fetchone()  # type: ignore[misc]

            print(f"接続ロール      : {user}")
            print(f"  rolsuper      : {is_super}")
            print(f"  rolbypassrls  : {bypass}")
            print(f"documents 所有者 : {owner}（接続ロールと同一か: {owner == user}）")
            print(f"RLS enabled     : {enabled}")
            print(f"RLS forced      : {forced}")

            cur.execute("SELECT set_config('app.tenant_id', %s, false)", (PROBE_A,))
            cur.execute("SELECT id FROM documents WHERE id IN (%s, %s) ORDER BY id",
                        (f"doc_{PROBE_A}", f"doc_{PROBE_B}"))
            visible = [r[0] for r in cur.fetchall()]
            print(f"\napp.tenant_id={PROBE_A} で見えたプローブ行: {visible}")
            isolated = visible == [f"doc_{PROBE_A}"]
            print("判定:", "RLS 有効（他テナントは見えない）" if isolated
                  else "★RLS バイパス（他テナントの行が見える）")

    with psycopg.connect(owner_dsn, autocommit=True) as oc, oc.cursor() as ocur:
        ocur.execute("DELETE FROM documents WHERE tenant_id IN (%s, %s)", (PROBE_A, PROBE_B))
        ocur.execute("DELETE FROM tenants WHERE id IN (%s, %s)", (PROBE_A, PROBE_B))
    print("(プローブ行は削除しました)")
    return 0 if isolated else 1


if __name__ == "__main__":
    raise SystemExit(main())
