"""FORCE ROW LEVEL SECURITY（§7.3）

0001 は ENABLE ROW LEVEL SECURITY までしか行っていなかった。PostgreSQL では
**テーブル所有者は RLS をバイパスする**ため、アプリが所有者ロールで繋いでいる限り
ポリシーは一切効かない。実 RDS で計測して確認した状態:

    接続ロール      : newfan   (rolsuper=False, rolbypassrls=False)
    documents 所有者 : newfan   ← 接続ロールと同一
    RLS enabled     : True
    RLS forced      : False
    app.tenant_id=ten_probe_a で見えた行: [doc_ten_probe_a, doc_ten_probe_b]  ← 他テナントが見える

FORCE で所有者にも RLS を適用する。所有者は migrate/バッチ専用にし、BYPASSRLS を
明示的に付けて横断バッチを通す（§7.3「マイグレーション/バッチ用ロールのみBYPASSRLS」）。
アプリは所有者でない newfan_app で繋ぐ（scripts/ensure_app_role.py が作成）。

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

# 0001 の _RLS_TABLES と一致させること。片方に足し忘れると、そのテーブルだけ
# 所有者バイパスが残る（テストが検知する）。
_RLS_TABLES = [
    "users",
    "field_schemas",
    "documents",
    "pages",
    "extraction_runs",
    "extraction_fields",
    "extraction_tables",
    "correction_logs",
    "tenant_memories",
    "tenant_rules",
    "jobs",
    "audit_logs",
    "connections",
    "workflows",
    "workflow_runs",
    "source_cursors",
]


def upgrade() -> None:
    for table in _RLS_TABLES:
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    # 所有者＝migrate/バッチ用ロール。FORCE を付けた以上、明示的に BYPASSRLS を
    # 与えないと横断バッチ（全テナントを跨ぐデータ移行など）が書けなくなる。
    # RDS のマスタは superuser ではないが rds_superuser 経由で BYPASSRLS を付与できる
    # （実 RDS で CREATE ROLE ... BYPASSRLS が通ることを確認済み）。
    # ローカル compose の postgres は既に superuser なので no-op。
    op.execute(
        """
        DO $$
        BEGIN
          EXECUTE format('ALTER ROLE %I BYPASSRLS', current_user);
        EXCEPTION WHEN insufficient_privilege THEN
          RAISE NOTICE 'BYPASSRLS を付与できませんでした（superuser なら不要）: %', SQLERRM;
        END $$;
        """
    )


def downgrade() -> None:
    for table in _RLS_TABLES:
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
