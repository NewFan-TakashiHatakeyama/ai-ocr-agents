"""接続の同期結果（⑤⑥ SaaS連携・敵対的レビュー確定所見の残課題）

「今すぐ同期」も定期ポーリングも結果は worker のログにしか残らず、利用者は
成功トースト（202 受付）しか見えない。folder_id 誤設定・API失敗・worker 停止の
いずれでも「取り込まれないのに気付けない」サイレント故障になるため、
最終同期の結果を connections に持たせて UI で可視化する。

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE connections
          ADD COLUMN IF NOT EXISTS last_synced_at   TIMESTAMPTZ,
          ADD COLUMN IF NOT EXISTS last_sync_status TEXT,
          ADD COLUMN IF NOT EXISTS last_sync_error  TEXT
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE connections
          DROP COLUMN IF EXISTS last_synced_at,
          DROP COLUMN IF EXISTS last_sync_status,
          DROP COLUMN IF EXISTS last_sync_error
        """
    )
