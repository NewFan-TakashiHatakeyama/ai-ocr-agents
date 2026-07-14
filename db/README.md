# db/ — Alembic マイグレーション

PostgreSQL スキーマの正本（§7 DDL / §7.3 RLS / §8.6 / §16.6）。詳細設計 §15「DBマイグレーションはAlembic」。

## マイグレーション

- `migrations/versions/0001_initial_schema.py` — 全テーブル（tenants/users/documents/pages/
  extraction_*/correction_logs/tenant_memories/tenant_rules/jobs/audit_logs、§16.6 の
  connections/workflows/workflow_runs/source_cursors）＋各テーブルの RLS ポリシー
  （`tenant_id = current_setting('app.tenant_id')`）。§8.6 の correction_logs.target_type/target_ref、
  §16.4 の jobs.kind（workflow/watch）も初期スキーマに含む。

RLS/CHECK/CITEXT を忠実に反映するため raw SQL（`op.execute`）で記述している。

## 使い方

```bash
export DATABASE_URL="postgresql://user:pass@localhost:5432/newfan"

# 適用
uv run alembic -c db/alembic.ini upgrade head

# SQL のみ生成（DB 接続なし・レビュー用）
uv run alembic -c db/alembic.ini upgrade head --sql > schema.sql

# 巻き戻し
uv run alembic -c db/alembic.ini downgrade -1
```

## 運用ノート

- gateway/orchestrator はリクエスト毎に `SET LOCAL app.tenant_id = '<tid>'` を発行して RLS を効かせる
  （マイグレーション/バッチ用ロールのみ BYPASSRLS）。
- audit_logs の append-only 化（UPDATE/DELETE 権限剥奪, §11）は、業務ロールへの GRANT 設計時に付与する。
- LangGraph チェックポイントは別スキーマ `langgraph`（PostgresSaver, §4.4）。本マイグレーションの対象外。
