"""P6 E2E: 「顧客基幹 DB」役の erp_demo スキーマを同一 RDS 内に用意する。

migrate タスク（所有者接続）で実行する前提。作るもの:
- スキーマ erp_demo + テーブル erp_demo.invoices
  （nf_write_key TEXT UNIQUE = insert モードの書込み台帳キー, §6.4）
- ロール erp_sink: INSERT/UPDATE/SELECT のみ（DD-12 の「専用ユーザー推奨」を演じる。
  newfan_app とは独立で、アプリ本体のテーブルには一切触れない）

env: DATABASE_URL（所有者）, SINK_PASSWORD（erp_sink のパスワード）
"""

from __future__ import annotations

import os

import psycopg
from psycopg import sql

PASSWORD = os.environ["SINK_PASSWORD"]

with psycopg.connect(os.environ["DATABASE_URL"].replace("+psycopg", ""), autocommit=True) as conn:
    conn.execute("CREATE SCHEMA IF NOT EXISTS erp_demo")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS erp_demo.invoices (
          id           BIGSERIAL PRIMARY KEY,
          invoice_no   TEXT,
          issuer_name  TEXT,
          amount       TEXT,
          source_system TEXT,
          nf_write_key TEXT UNIQUE,
          created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    # upsert（ON CONFLICT (invoice_no) DO UPDATE）用の一意キー
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_erp_invoices_no"
        " ON erp_demo.invoices (invoice_no)"
    )
    conn.execute(
        """
        DO $$ BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='erp_sink') THEN
            CREATE ROLE erp_sink LOGIN;
          END IF;
        END $$;
        """
    )
    conn.execute(sql.SQL("ALTER ROLE erp_sink LOGIN PASSWORD {}").format(sql.Literal(PASSWORD)))
    conn.execute("GRANT USAGE ON SCHEMA erp_demo TO erp_sink")
    # INSERT/UPDATE は db_write（upsert）に必要。SELECT は ON CONFLICT の既存行参照と
    # 検証用。DELETE は与えない（DD-12: 顧客側は書込み専用ユーザーを推奨）
    conn.execute("GRANT SELECT, INSERT, UPDATE ON erp_demo.invoices TO erp_sink")
    conn.execute("GRANT USAGE ON SEQUENCE erp_demo.invoices_id_seq TO erp_sink")
print("sink demo ok: erp_demo.invoices + role erp_sink")
