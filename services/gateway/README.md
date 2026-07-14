# newfan-gateway

gateway-api: REST / 認証 / HITL API（詳細設計 §6）。FastAPI。

## 実装済みエンドポイント（§6.2）

| メソッド | パス | 権限 |
|---|---|---|
| POST | /v1/documents | uploader+ |
| GET | /v1/documents | viewer+ |
| GET | /v1/documents/{id} | viewer+ |
| POST | /v1/documents/{id}/extract | uploader+ |
| GET | /v1/jobs/{id} | viewer+ |
| GET | /v1/documents/{id}/result | viewer+ |
| GET | /v1/documents/{id}/pages/{n}/image | viewer+ |
| POST | /v1/documents/{id}/corrections | reviewer+ |
| POST | /v1/documents/{id}/confirm | reviewer+ |
| GET | /v1/review/queue | reviewer+ |

共通仕様（§6.1）: `X-Request-Id` 付与、エラー形式 `{"error":{code,message,details,request_id}}`、
`Idempotency-Key`（extract/confirm）、カーソルページング、JWT / `X-API-Key` 認証、RBAC 階層。

## アーキテクチャ（差し込み式）

エンドポイントは Protocol にのみ依存し、実装を注入する。

| Port | テスト/dev | 本番 |
|---|---|---|
| Repository | `InMemoryRepository` | `db.PgRepository`（PostgreSQL + RLS） |
| Queue | `InMemoryQueue` | `prod.RedisQueue`（Redis Streams, DD-05） |
| OrchestratorClient | `FakeOrchestratorClient` | `prod.HttpOrchestratorClient`（resume 内部RPC, §4.4） |
| Ingestor | `IngestService`（fake rasterizer） | `IngestService`（pypdfium2 + S3） |

`create_app(repo=..., queue=..., orchestrator=..., ingestor=..., api_keys=...)` で注入。
未指定なら In-Memory（dev）/ 本番構成を既定構築。

## テナント分離（§11）

- JWT/APIキー → `tenant_id` 解決、全リポジトリ操作で tenant スコープ。
- 本番 `PgRepository` はリクエスト毎に `SET LOCAL app.tenant_id` を発行し RLS を効かせる。

## 実行

```bash
uv run pytest services/gateway                 # In-Memory で E2E（DB/Redis 不要）
uv run --extra runtime uvicorn newfan_gateway.app:create_app --factory --port 8000
```

本番は `db`（sqlalchemy/asyncpg）・`redis` を含む runtime extra が必要。テーブル正本は
Alembic マイグレーション（§15）。`db.py` の ORM は gateway が参照する列のミラー。

## 未実装（TODO）

- schemas / memory / rules / webhooks エンドポイント（§6.2 の admin 系）
- POST /chat（SSE, §6.3）
- レート制限（§6.1, 429 + Retry-After）
- 署名 URL の実発行（現状は image_uri をそのまま返す。S3 presign に差し替え）
