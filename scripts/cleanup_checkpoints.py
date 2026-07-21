"""lg_wf checkpoint の掃除（§16 設計 v0.2 §6.6 / P8）。

PostgresSaver に TTL は無い（backdate した checkpoint からの resume 成功を実測済み）。
終端（succeeded/failed/skipped）から 30 日経過した workflow_run の thread を
lg_wf スキーマから削除する。migrate タスク（所有者接続）で `up` のたびに実行する
（設計は「up 時 + 週次（稼働中のみ）」。本運用は使う時だけ up なので up 時で足りる。
常時稼働に移行したら週次実行を追加すること）。

workflow_runs 自体（観測用射影）は消さない（保持は §7.4 に従う）。

env: DATABASE_URL（所有者）, RETENTION_DAYS（既定 30）
"""

from __future__ import annotations

import os

import psycopg

RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "30"))

with psycopg.connect(os.environ["DATABASE_URL"].replace("+psycopg", "")) as conn:
    total = 0
    for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
        cur = conn.execute(
            f"""
            DELETE FROM lg_wf.{table}
            WHERE thread_id IN (
              SELECT id FROM workflow_runs
              WHERE status IN ('succeeded','failed','skipped')
                AND finished_at < now() - interval '{RETENTION_DAYS} days'
            )
            """
        )
        total += cur.rowcount
    conn.commit()
print(f"checkpoint cleanup ok: {total} rows deleted (retention={RETENTION_DAYS}d)")
