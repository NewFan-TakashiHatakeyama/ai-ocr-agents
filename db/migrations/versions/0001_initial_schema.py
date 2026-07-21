"""initial schema (§7 DDL / §7.3 RLS / §8.6 / §16.6)

Revision ID: 0001
Revises:
Create Date: 2026-07-14
"""

from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# RLS を適用するテナントスコープテーブル（§7.3「全業務テーブル」）
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

_CORE = """
CREATE EXTENSION IF NOT EXISTS citext;

CREATE TABLE tenants (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  plan        TEXT NOT NULL DEFAULT 'standard',
  settings    JSONB NOT NULL DEFAULT '{}',
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE users (
  id          TEXT PRIMARY KEY,
  tenant_id   TEXT NOT NULL REFERENCES tenants(id),
  email       CITEXT NOT NULL UNIQUE,
  role        TEXT NOT NULL CHECK (role IN ('admin','reviewer','uploader','viewer')),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE field_schemas (
  id          TEXT PRIMARY KEY,
  tenant_id   TEXT NOT NULL REFERENCES tenants(id),
  doc_type    TEXT NOT NULL,
  version     INT  NOT NULL,
  fields      JSONB NOT NULL,
  is_active   BOOLEAN NOT NULL DEFAULT true,
  created_by  TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, doc_type, version)
);

CREATE TABLE documents (
  id            TEXT PRIMARY KEY,
  tenant_id     TEXT NOT NULL REFERENCES tenants(id),
  storage_uri   TEXT NOT NULL,
  original_name TEXT,
  mime_type     TEXT NOT NULL,
  page_count    INT,
  doc_type      TEXT,
  external_ref  TEXT,
  status        TEXT NOT NULL DEFAULT 'uploaded'
    CHECK (status IN ('uploaded','queued','processing','needs_review',
                      'in_review','confirmed','exported','failed')),
  uploaded_by   TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_documents_tenant_status ON documents (tenant_id, status, created_at DESC);
CREATE INDEX idx_documents_external ON documents (tenant_id, external_ref);

CREATE TABLE pages (
  id          TEXT PRIMARY KEY,
  tenant_id   TEXT NOT NULL,
  document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  page_no     INT NOT NULL,
  width       INT, height INT,
  image_uri   TEXT NOT NULL,
  preproc     JSONB NOT NULL DEFAULT '{}',
  UNIQUE (document_id, page_no)
);

CREATE TABLE extraction_runs (
  id               TEXT PRIMARY KEY,
  tenant_id        TEXT NOT NULL,
  document_id      TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  schema_id        TEXT REFERENCES field_schemas(id),
  status           TEXT NOT NULL DEFAULT 'processing'
    CHECK (status IN ('processing','needs_review','confirmed','failed','superseded')),
  engine_versions  JSONB NOT NULL,
  options          JSONB NOT NULL DEFAULT '{}',
  metrics          JSONB NOT NULL DEFAULT '{}',
  result_version   INT NOT NULL DEFAULT 1,
  started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at      TIMESTAMPTZ
);
CREATE INDEX idx_runs_doc ON extraction_runs (document_id, started_at DESC);

CREATE TABLE extraction_fields (
  id               TEXT PRIMARY KEY,
  tenant_id        TEXT NOT NULL,
  run_id           TEXT NOT NULL REFERENCES extraction_runs(id) ON DELETE CASCADE,
  field_name       TEXT NOT NULL,
  value_raw        TEXT,
  value_normalized TEXT,
  final_value      TEXT,
  confidence       REAL NOT NULL DEFAULT 0,
  grounding_score  REAL NOT NULL DEFAULT 0,
  page_no          INT,
  bbox             JSONB,
  char_boxes       JSONB,
  source_quote     TEXT,
  span_ids         JSONB,
  correction       JSONB,
  validation       JSONB,
  review_status    TEXT NOT NULL DEFAULT 'auto'
    CHECK (review_status IN ('auto','pending','corrected','approved')),
  UNIQUE (run_id, field_name)
);
CREATE INDEX idx_fields_run ON extraction_fields (run_id);
CREATE INDEX idx_fields_review ON extraction_fields (tenant_id, review_status)
  WHERE review_status = 'pending';

CREATE TABLE extraction_tables (
  id            TEXT PRIMARY KEY,
  tenant_id     TEXT NOT NULL,
  run_id        TEXT NOT NULL REFERENCES extraction_runs(id) ON DELETE CASCADE,
  name          TEXT NOT NULL,
  page_no       INT,
  structure_html TEXT,
  rows          JSONB NOT NULL,
  confidence    REAL
);

-- §8.6: target_type / target_ref を初期スキーマに含める
CREATE TABLE correction_logs (
  id              TEXT PRIMARY KEY,
  tenant_id       TEXT NOT NULL,
  document_id     TEXT NOT NULL,
  run_id          TEXT NOT NULL,
  field_name      TEXT NOT NULL,
  target_type     TEXT NOT NULL DEFAULT 'field',
  target_ref      TEXT,
  original_value  TEXT,
  corrected_value TEXT NOT NULL,
  doc_type        TEXT,
  supplier_key    TEXT,
  context         TEXT,
  reviewer_id     TEXT,
  embedded        BOOLEAN NOT NULL DEFAULT false,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_corrections_pattern ON correction_logs
  (tenant_id, doc_type, field_name, created_at DESC);

CREATE TABLE tenant_memories (
  id                TEXT PRIMARY KEY,
  tenant_id         TEXT NOT NULL,
  correction_log_id TEXT NOT NULL REFERENCES correction_logs(id) ON DELETE CASCADE,
  faiss_vector_id   BIGINT NOT NULL,
  embed_model       TEXT NOT NULL,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, faiss_vector_id)
);

CREATE TABLE tenant_rules (
  id                 TEXT PRIMARY KEY,
  tenant_id          TEXT NOT NULL,
  doc_type           TEXT,
  supplier_key       TEXT,
  field_name         TEXT,
  rule_type          TEXT NOT NULL
    CHECK (rule_type IN ('regex_replace','vocab_map','format','checksum','llm_hint')),
  rule_json          JSONB NOT NULL,
  status             TEXT NOT NULL DEFAULT 'draft'
    CHECK (status IN ('draft','validating','active','retired')),
  validation_report  JSONB,
  source_correction_ids JSONB,
  created_by         TEXT NOT NULL,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_rules_lookup ON tenant_rules (tenant_id, status, doc_type, field_name);

-- §16.4: jobs.kind に 'workflow','watch' を追加
CREATE TABLE jobs (
  id           TEXT PRIMARY KEY,
  tenant_id    TEXT NOT NULL,
  kind         TEXT NOT NULL
    CHECK (kind IN ('extract','vl','learn','export','rule_extract','workflow','watch')),
  ref_id       TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued','running','succeeded','failed','dead')),
  attempt      INT NOT NULL DEFAULT 0,
  error_code   TEXT,
  payload      JSONB NOT NULL DEFAULT '{}',
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at   TIMESTAMPTZ,
  finished_at  TIMESTAMPTZ
);
CREATE INDEX idx_jobs_status ON jobs (status, kind, created_at);

CREATE TABLE audit_logs (
  id          TEXT PRIMARY KEY,
  tenant_id   TEXT NOT NULL,
  actor_type  TEXT NOT NULL CHECK (actor_type IN ('human','agent','system')),
  actor_id    TEXT,
  action      TEXT NOT NULL,
  target_type TEXT NOT NULL,
  target_id   TEXT NOT NULL,
  detail      JSONB NOT NULL DEFAULT '{}',
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_audit ON audit_logs (tenant_id, created_at DESC);
"""

# §16.6 ワークフロー自動化テーブル
_WORKFLOW = """
CREATE TABLE connections (
  id             TEXT PRIMARY KEY,
  tenant_id      TEXT NOT NULL,
  type           TEXT NOT NULL,
  name           TEXT NOT NULL,
  config         JSONB NOT NULL DEFAULT '{}',
  secret_ref     TEXT,
  allowed_tables JSONB,
  status         TEXT NOT NULL DEFAULT 'untested',
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE workflows (
  id           TEXT PRIMARY KEY,
  tenant_id    TEXT NOT NULL,
  name         TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'draft'
    CHECK (status IN ('draft','active','paused','retired')),
  version      INT NOT NULL DEFAULT 1,
  graph_json   JSONB NOT NULL,
  auto_confirm BOOLEAN NOT NULL DEFAULT false,
  created_by   TEXT,
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE workflow_runs (
  id               TEXT PRIMARY KEY,
  tenant_id        TEXT NOT NULL,
  workflow_id      TEXT NOT NULL REFERENCES workflows(id),
  workflow_version INT NOT NULL,
  trigger          JSONB NOT NULL,
  document_id      TEXT,
  state            JSONB NOT NULL DEFAULT '{}',
  status           TEXT NOT NULL DEFAULT 'running'
    CHECK (status IN ('running','waiting_hitl','succeeded','failed','skipped')),
  error            JSONB,
  started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at      TIMESTAMPTZ
);
CREATE INDEX idx_wfruns ON workflow_runs (tenant_id, workflow_id, started_at DESC);

CREATE TABLE source_cursors (
  id            TEXT PRIMARY KEY,
  tenant_id     TEXT NOT NULL,
  connection_id TEXT NOT NULL,
  source_key    TEXT NOT NULL,
  content_hash  TEXT NOT NULL,
  workflow_id   TEXT,
  processed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, connection_id, source_key, content_hash)
);
"""


def upgrade() -> None:
    op.execute(_CORE)
    op.execute(_WORKFLOW)
    # §7.3 RLS: 全業務テーブルで tenant_id = current_setting('app.tenant_id')
    for table in _RLS_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            "USING (tenant_id = current_setting('app.tenant_id', true))"
        )
    # audit_logs は append-only（UPDATE/DELETE を業務ロールから剥奪, §11）は
    # ロール付与時に運用（GRANT）。ここではテーブル定義のみ。


def downgrade() -> None:
    for table in _RLS_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    for table in [
        "source_cursors", "workflow_runs", "workflows", "connections",
        "audit_logs", "jobs", "tenant_rules", "tenant_memories", "correction_logs",
        "extraction_tables", "extraction_fields", "extraction_runs", "pages",
        "documents", "field_schemas", "users", "tenants",
    ]:
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
