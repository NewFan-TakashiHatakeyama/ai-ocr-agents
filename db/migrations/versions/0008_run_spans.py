"""run_spans（run ごとの OCR span の永続化。設計 D12 / §2.4）

読取領域ヒント（docs/design/region-field-add-and-hint-v2.md）の土台。

- 新規項目に領域を引いたとき「この枠に何が書いてあるか」をその場で見せる
  （いまは保存→再抽出で初めて分かる）。
- 例示値（`RegionRect.example_value`）の出どころは span の原文に限る（D11）ので、
  run が終わった後も span を引ける場所が要る。structure-svc に都度問い合わせる案は
  採らない（ページあたり数秒〜数十秒。矩形を引くたびに待たせるのは操作にならない）。

1 行 = 1 run × 1 ページ。spans は `[{span_id, text, bbox, conf}]` の JSONB
（1 ページ数百 span × 100 バイト ≒ 数十 KB／ページ。圧縮や別ストアは入れない）。
PK (run_id, page_no) が「再配信で二重に増えない」ための土台
（save_result 側は ON CONFLICT ... DO UPDATE）。

extraction_runs への FK CASCADE で、帳票削除（documents → extraction_runs）に
連鎖して消える。delete_document の手順は変えない。

RLS は 0003 と同じく ENABLE + FORCE（アプリは非所有ロール newfan_app で接続する前提）。
GRANT は scripts/ensure_app_role.py の一括 GRANT + ALTER DEFAULT PRIVILEGES で付く
（表ごとの明示リストは 0003 の足し忘れ事故で廃止済み）。

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE run_spans (
          run_id     TEXT NOT NULL REFERENCES extraction_runs(id) ON DELETE CASCADE,
          tenant_id  TEXT NOT NULL,
          page_no    INT NOT NULL,
          spans      JSONB NOT NULL,
          PRIMARY KEY (run_id, page_no)
        );
        """
    )
    op.execute("ALTER TABLE run_spans ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE run_spans FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON run_spans "
        "USING (tenant_id = current_setting('app.tenant_id', true))"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS run_spans")
