"""ドキュメント削除（DELETE /v1/documents/{id}）の土台

削除で消えるのは FK CASCADE が張られた表だけで、documents を TEXT 列で指すだけの
correction_logs・jobs・workflow_runs は DB が何もしてくれず孤児になる（0001 の
:154 / :206 / :265）。アプリ側で明示 DELETE するが、索引が無いと 1 件消すたびに
seq scan になるため索引を先に用意する。CASCADE 側（extraction_tables.run_id /
tenant_memories.correction_log_id）にも索引が無いので合わせて張る。

correction_logs にだけ FK を足すのは、learn ジョブが「削除済み帳票の修正例」を
後から作れてしまい、消したはずの原本の値（original_value / corrected_value）が
DB に復活するため。NOT VALID にして既存の孤児は触らない（VALIDATE すると
migration が無音で既存データを壊す。scripts/e2e_real.py:86 の所有者接続による
DELETE FROM documents で実在し得る）。NOT VALID でも RI トリガは張られるので
「新規 INSERT の拒否」と「親削除時の CASCADE」は両方効く。

**この migration は途中で失敗しても再実行できなければならない。**
op.get_context().autocommit_block() は直前のトランザクションを無条件に commit する。
つまり ADD CONSTRAINT は CREATE INDEX CONCURRENTLY より先に確定する一方、
alembic_version が 0005 に進むのは upgrade() が正常終了した後だけ。素朴に書くと
索引作成が 1 回でも落ちた瞬間に「FK はあるのに版数は 0004」で固まり、以後の
upgrade が duplicate_object で永久に失敗する（migrate タスクが恒久的に exit!=0 に
なり、aws_env.sh の up が二度と通らなくなる）。さらに失敗した CONCURRENTLY は
indisvalid=false の索引を残し、IF NOT EXISTS がそれを飛ばし続けるので索引も
永久に無効のままになる。そのため:
  - ADD CONSTRAINT は存在チェック付きで冪等にする
  - CREATE の前に「無効な同名索引」を落としてから張り直す

新規列は追加しない。よって migrate タスクと gateway サービスの更新順序に
依存しない（旧 gateway が新スキーマで壊れることも、新 gateway が旧スキーマで
UndefinedColumn を出すこともない）。

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

# (索引名, 定義)。CONCURRENTLY は autocommit_block の中でしか使えない。
_INDEXES = [
    # 明示 DELETE する表（FK CASCADE が無い＝アプリが消す）
    ("idx_correction_logs_document", "correction_logs (tenant_id, document_id)"),
    # jobs は document_id を持たず ref_id が run_id を指す（0001:206）
    ("idx_jobs_ref", "jobs (tenant_id, ref_id)"),
    (
        "idx_wfruns_document",
        "workflow_runs (tenant_id, document_id) WHERE document_id IS NOT NULL",
    ),
    # CASCADE 側。親削除時に PostgreSQL が子を引くので、ここに索引が無いと
    # 1 件削除のたびに全表走査になる（extraction_fields/pages は 0001 で既にある）
    ("idx_extraction_tables_run", "extraction_tables (run_id)"),
    ("idx_tenant_memories_correction", "tenant_memories (correction_log_id)"),
]


def upgrade() -> None:
    # 冪等な ADD CONSTRAINT。ALTER TABLE ... ADD CONSTRAINT に IF NOT EXISTS は無い。
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'correction_logs_document_fk'
          ) THEN
            ALTER TABLE correction_logs
              ADD CONSTRAINT correction_logs_document_fk
              FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE
              NOT VALID;
          END IF;
        END $$
        """
    )

    # 索引は CONCURRENTLY で張る。env.py はトランザクション内で run_migrations()
    # するため、素の CREATE INDEX は対象表への書き込みをビルド完了までブロックする
    # （＝デプロイのたびに取込が止まる）。
    with op.get_context().autocommit_block():
        for name, definition in _INDEXES:
            # 前回の失敗が残した無効な索引を落としてから張り直す。
            # これが無いと IF NOT EXISTS が無効索引を飛ばし続け、永久に効かない。
            op.execute(
                f"""
                DO $$
                BEGIN
                  IF EXISTS (
                    SELECT 1 FROM pg_class c
                    JOIN pg_index i ON i.indexrelid = c.oid
                    WHERE c.relname = '{name}' AND NOT i.indisvalid
                  ) THEN
                    EXECUTE 'DROP INDEX IF EXISTS {name}';
                  END IF;
                END $$
                """
            )
            op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {definition}")


def downgrade() -> None:
    # DROP は CONCURRENTLY にしない。downgrade は手動・稀な経路で、索引削除の
    # 排他ロックは一瞬。CONCURRENTLY にすると autocommit_block が要るうえ、
    # 他セッションの完了待ちで無言のまま止まり得る（実測）。
    for name, _ in reversed(_INDEXES):
        op.execute(f"DROP INDEX IF EXISTS {name}")
    op.execute(
        "ALTER TABLE correction_logs DROP CONSTRAINT IF EXISTS correction_logs_document_fk"
    )
