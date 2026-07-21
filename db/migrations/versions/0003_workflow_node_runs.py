"""workflow_node_runs（§16 設計 v0.2 §3.2）

ノード単位の実行履歴。POST /workflow-runs/{id}/retry（失敗ノードからの再実行）と
workflow_node_duration_seconds（§16.8）の出所になる。workflow_runs.state（JSONB）だけだと
「どのノードが何回試行してどう失敗したか」を構造で引けない。

RLS は 0002 と同じく ENABLE + FORCE（アプリは非所有ロール newfan_app で接続する前提）。

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE workflow_node_runs (
          id               TEXT PRIMARY KEY,
          tenant_id        TEXT NOT NULL,
          workflow_run_id  TEXT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
          node_id          TEXT NOT NULL,
          node_type        TEXT NOT NULL,
          status           TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending','running','succeeded','failed','skipped')),
          attempt          INT NOT NULL DEFAULT 0,
          input            JSONB,
          output           JSONB,
          error            JSONB,
          started_at       TIMESTAMPTZ,
          finished_at      TIMESTAMPTZ,
          UNIQUE (workflow_run_id, node_id)
        );
        CREATE INDEX idx_wf_node_runs ON workflow_node_runs (tenant_id, workflow_run_id);
        """
    )
    op.execute("ALTER TABLE workflow_node_runs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE workflow_node_runs FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON workflow_node_runs "
        "USING (tenant_id = current_setting('app.tenant_id', true))"
    )


def downgrade() -> None:
    op.execute("DROP TABLE workflow_node_runs")
