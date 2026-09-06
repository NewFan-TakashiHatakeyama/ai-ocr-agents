"""field_schemas.exclude_regions / source_page_count（領域指定テンプレート化）

領域指定テンプレート化（docs/design/region-template-editor.md）の保存契約。

- exclude_regions: 読み取りたくない領域（印影・ロゴ等）の RegionRect 配列。
  fields JSONB 内に擬似フィールドとして置かない（classify の分類語彙を汚染し、
  スキーマ編集画面にも並んでしまうため。設計 §4.3）。NOT NULL DEFAULT '[]' に
  するのは、読み手全員が「bare 配列を舐める」前提でコードを書けるようにするため。
- source_page_count: テンプレート化した時点の帳票ページ数。位置ガードが
  「run のページ数が違うなら page 不一致を咎めない」判定に使う（設計 §5.5）。
  過去版には無いので nullable。

0006 と同じ「ADD COLUMN IF NOT EXISTS + DEFAULT」パターン。migrate を
コード配備より先に流せば、旧 gateway/orchestrator が動いたままでも安全
（新列は誰も読まない）。

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE field_schemas "
        "ADD COLUMN IF NOT EXISTS exclude_regions JSONB NOT NULL DEFAULT '[]'::jsonb"
    )
    op.execute("ALTER TABLE field_schemas ADD COLUMN IF NOT EXISTS source_page_count INT")


def downgrade() -> None:
    op.execute("ALTER TABLE field_schemas DROP COLUMN IF EXISTS source_page_count")
    op.execute("ALTER TABLE field_schemas DROP COLUMN IF EXISTS exclude_regions")
