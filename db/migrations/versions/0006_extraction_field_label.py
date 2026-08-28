"""extraction_fields.label（スキーマレス自動発見の見出し原文）

スキーマ指定の抽出では label は field_schemas から都度引けるため列は不要だった。
スキーマレス抽出（§5.5.1: 先に取り込んで値を見てからテンプレート化する導線）では
LLM が発見した見出し原文（「御請求金額」等）が唯一の表示名であり、これを
永続化しないと検証画面もテンプレート化ダイアログも snake_case の name しか
出せなくなる。nullable の追記のみ（スキーマ指定の既存経路は NULL のまま）。

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE extraction_fields ADD COLUMN IF NOT EXISTS label TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE extraction_fields DROP COLUMN IF EXISTS label")
