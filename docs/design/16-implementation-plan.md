# §16 エージェントワークフロー 実装方針 v1.0

- 日付: 2026-07-21
- 前提: [16-agent-workflow.md](16-agent-workflow.md) v0.2（Approved）/ 実測 4 項目済み
- 原則: 各 Phase は**単体で価値がある縦切り**。実 AWS で動作確認してから次へ進む
  （本プロジェクトの既定。fixture で動いても実系で死ぬ事例を繰り返し踏んでいる）

## 0. 確定済みの技術判断（再議論しない）

| 判断 | 根拠 |
|---|---|
| エンジンは LangGraph 1.2.9 + PostgresSaver 3.1.0（既存資産） | ライセンス/常駐で既製全滅・実測 4 項目 OK |
| checkpointer は `lg_wf` スキーマに分離（接続文字列に search_path を焼く） | DD-11。実測で分離確認 |
| runner / consumer は `newfan_app`（非所有ロール）で接続 | RLS。実測で動作確認 |
| トリガーは S3→EventBridge→SQS(14日)→consumer。常駐なし | 月 $28 運用と両立 |
| エディタは React Flow + rjsf（catalog() の JSON Schema 駆動） | MIT・フォーム手書き回避 |
| 冪等規則は「完了ノードは再実行されない / interrupt 前コードは再実行される」に基づく | 実測 3' |

---

## P2: ワークフロー管理 API（次の着手）

**ゴール**: SCR-07 なしで、JSON 直投入により workflow を保存 → lint → 有効化できる。

### 変更ファイル

| ファイル | 内容 |
|---|---|
| `db/migrations/versions/0003_workflow_node_runs.py` | 設計 §3.2 の DDL + RLS(ENABLE+FORCE) + 0001/0002 の `_RLS_TABLES` 整合 |
| `db/tests/test_rls_isolation.py` | 対象テーブルに workflow_node_runs を追加 |
| `services/gateway/src/newfan_gateway/records.py` | `WorkflowRecord`（id, name, status, version, graph_json, auto_confirm）/ `WorkflowRunRecord` |
| `services/gateway/src/newfan_gateway/workflows_repo.py`（新規） | Protocol + InMemory + Pg。**版管理**: PUT は新版 INSERT（`UNIQUE(id, version)` 相当は workflows が最新のみ保持し、旧版は `workflow_versions` に…ではなく v1.2 準拠で workflows.graph_json を更新し版番号 increment、実行中 run は `workflow_runs.workflow_version` + 発行時の graph_json スナップショットを `workflow_runs.trigger` に保存） |
| `services/gateway/src/newfan_gateway/dto.py` | Workflow 系 DTO（graph_json は `newfan_workflow.WorkflowGraph` で検証してから dict 保存） |
| `services/gateway/src/newfan_gateway/routers.py` | GET/POST `/workflows`, GET/PUT `/workflows/{id}`, POST `activate/pause/lint`, GET `/workflows/catalog` |
| `services/gateway/pyproject.toml` | `newfan-workflow` 依存追加 |

### 契約の要点

- 保存時: `WorkflowGraph.model_validate` が通らなければ 422（E4001）。lint は**保存を妨げない**
  （error があっても draft 保存は可。activate だけが error ゼロを要求）
- activate: lint(error=0) + capability 検査（P 未実装ノード type の拒否リスト。P3 時点では
  `source.manual / process.extract / branch.condition / transform.map_fields / sink.webhook` のみ許可）
- L009/L010 の resolver は admin repo（schema）と connections から注入
- audit_logs へ activate/pause を記録

### DoD

- 実 AWS: 保存 → lint 指摘確認 → 修正 → activate 成功 → pause
- 実 PG 結合テスト（InMemory と Pg 両方）: 版 increment / activate 条件 / RLS 分離
- CI 緑

規模: 中（既存 CRUD パターンの踏襲）。

---

## P3: workflow-runner（本体）

**ゴール**: 実 AWS で `POST /workflows/{id}/runs` → 抽出 → 分岐 → webhook が端から端まで動く。
セグメント途中でタスクを kill しても resume で完走する。

### 変更ファイル

| ファイル | 内容 |
|---|---|
| `services/orchestrator/src/newfan_orchestrator/workflow_graph.py`（新規） | graph_json → StateGraph 構築。`WorkflowState`、ノード実装レジストリ、`add_conditional_edges` + expr、compile キャッシュ dict、`workflow_node_runs` 記録ラッパ |
| `services/orchestrator/src/newfan_orchestrator/workflow_runner.py`（新規） | q.workflow consumer。行ロック（FOR UPDATE SKIP LOCKED）→ invoke/resume → interrupt 分類 → projection 更新 → ack。`RetryPolicy` 設定 |
| `services/orchestrator/src/newfan_orchestrator/workflow_store.py`（新規） | workflow_runs / workflow_node_runs の Pg アクセス（PgContextStore と同パターン、アプリロール） |
| `services/orchestrator/src/newfan_orchestrator/worker.py` | 完了 notify: payload に `notify` があれば `{"type":"resume", ...}` を指定 stream へ enqueue（約 10 行。ワークフローの概念は持ち込まない） |
| `services/orchestrator/src/newfan_orchestrator/worker_main.py` | lg_wf 用 PostgresSaver（`DATABASE_URL` + search_path オプション）と runner consumer の起動 |
| `scripts/setup_checkpointer.py` | lg_wf スキーマ作成 + search_path 付き setup() + GRANT を追加 |
| `services/gateway/src/newfan_gateway/routers.py` | POST `/workflows/{id}/runs`, GET runs/一覧/詳細, POST retry |
| `packages/metrics` | `workflow_runs_total` / `workflow_node_duration_seconds` |

### ノード実装（P3 スコープ）

| type | 実装 |
|---|---|
| `source.manual` | no-op（run 生成時に document_id を state に積む） |
| `process.extract` | 冪等キー付きで抽出 Run を発行（gateway 経由でなく repo/queue 直。`Idempotency-Key = workflow_run_id:node_id` 相当の既存 Run 再利用チェック）→ `interrupt({kind:"await_extract"})` → resume event から `run_status` / `fields` 断面を state へ |
| `branch.condition` | conditional edges（P1 の evaluate。ctx は state から組む） |
| `transform.map_fields` | 純関数（from/const/format/mask） |
| `sink.webhook` | 既存 `newfan_export.webhook` の署名・SSRF・失敗計数を流用。イベント id = `workflow_run_id:node_id` |

### テスト

- プローブ 4 本を `services/orchestrator/tests/test_workflow_runner_pg.py` に昇格
  （DATABASE_URL_TEST ゲート）: スキーマ分離 / 非所有ロール / kill→resume / 再実行境界
- InMemory + Fake 抽出でのグラフ配線テスト（needs_review 分岐・else 分岐・webhook 発火）
- 実 AWS E2E（DoD）: sample2.png で manual 実行 → needs_review 経路、確定後に webhook 受信

規模: 大（ここが本体）。

---

## P4: S3 イベント駆動トリガー + DD-13

### 変更ファイル

| ファイル | 内容 |
|---|---|
| `deploy/terraform/workflow_trigger.tf`（新規） | S3 EventBridge 通知 ON / EventBridge ルール（bucket+prefix）/ SQS + DLQ（保持 14 日）/ IAM（consumer の sqs:ReceiveMessage 等）。**SQS と EventBridge ルールは ECR 同様 down で消さない別スタック**に置くか要判断 → 消えるとイベントを取りこぼすため **ecr/ と同じ長命スタックへ** |
| `services/orchestrator/src/newfan_orchestrator/trigger_consumer.py`（新規） | SQS 購読（up 中のみ）。ETag で source_cursors INSERT ON CONFLICT → skip or ingest→documents→workflow_runs→q.workflow |
| `services/ingest` | 取込関数の流用（S3 オブジェクト取得 → ingest → ページ登録） |
| `scripts/aws_env.sh` | `up` の最後に SQS 滞留数を表示（drain の見える化） |

### DoD

- 実 AWS: S3 に帳票を置く → 自動で抽出される（稼働中、数秒〜）
- 同一ファイル再配置 → skip（source_cursors）
- **down 中に置く → up → 自動 drain されて処理される**（運用の核心。実測）

規模: 中。

---

## P5: HITL ゲート

- `branch.hitl_gate` ノード実装（needs_review のときのみ `interrupt({kind:"await_hitl"})`、
  priority_boost をレビューキューへ）
- 確定フロー完了時の notify は P3 の同一機構（ワーカー完了 notify）で発火するため、
  **新規配線はノード実装のみ**
- DoD: 実 AWS で needs_review → SCR-03 で確定 → ワークフローが下流継続

規模: 小〜中（P3 の資産に乗る）。

---

## P6: sink.db_write（DD-12）+ dry-run + connections

- `workflow_sinks.py`: allowed_tables 完全一致 → `psycopg.sql.Identifier` → プリペアド
  INSERT/UPSERT → 行数上限 1,000 → 書込み台帳（冪等）
- `POST /workflows/{id}/dry-run`: sink を実行せず SQL/ペイロードのプレビュー返却。
  activate の前提条件に組み込み
- `POST /connections` + `/connections/{id}/test`（DB 疎通・SELECT 1 のみ）+
  webhook secret → secret_ref 移行
- DoD: 実 PG（顧客 DB 想定のローカル別 DB）で dry-run プレビュー = 実 SQL、
  allowed_tables 外の拒否・行数超過の拒否がテストで固定

規模: 中〜大（安全策が厚い）。

---

## P7: SCR-07 ノードエディタ

1. **PoC 先行**（1〜2 日で判断）: React Flow + rjsf + `GET /workflows/catalog` で
   ノード配置 → config フォーム自動生成 → graph_json 生成 → `/workflows/{id}/lint` 往復。
   rjsf の from/const 排他（anyOf）が素で通るかをここで確認
2. 本実装: SCR-07 ページ（web/app/workflows/）。custom node は memo 化。
   lint 指摘の node_id ハイライト、activate ボタンは error=0 で活性
- DoD: catalog 由来フォームで 13 種すべての config を編集 → 保存 → activate

規模: 大（UI）。

---

## P8: 残り

schedule（EventBridge Scheduler → SQS）/ classify / notify(Slack) / email /
checkpoint 掃除（up 時 + 週次）/ `sink_write_rows_total` `watch_lag_seconds` /
Langfuse 紐付け。

---

## リスクと手当て

| リスク | 手当て |
|---|---|
| search_path 分離の運用ミス（プール共有で lg_wf/public が混ざる） | 接続文字列に焼く方式のみ許可（SET 方式は禁止）。P3 の結合テストが分離を常時検証 |
| interrupt 前の副作用の二重実行 | 実測済みの境界に基づく冪等キー（§6.4 の表）。extract は既存 idempotency 機構を流用 |
| 長命スタック（SQS）と down 運用の整合 | SQS/EventBridge は ecr/ と同じ「down で消さない」スタックへ。up 時に滞留数を表示 |
| LangGraph のバージョン更新で checkpoint 形式が変わる | `langgraph-checkpoint-postgres` はマイグレーション内蔵（checkpoint_migrations 表）。更新は minor 固定 + ゴールデン回帰と同時にのみ |
| capability 検査漏れ（未実装ノードが有効化される） | activate の許可リストは Phase ごとにテストで固定 |

## 直近の作業順

1. **P1 微修正**: `source.folder_watch` → `source.s3_event`（設計 v0.2 と整合）
2. **P2**: migration 0003 → repos → API → 実 AWS 確認
3. **P3**: builder → runner → notify 配線 → 手動実行 API → 実 AWS E2E + kill/resume 実測
4. 以降 P4 → P5 → P6 → P7 → P8

P2+P3 が終わった時点で「保存 → 有効化 → 手動実行 → 抽出 → 分岐 → 配信」の
1 本が実 AWS で動く。ここが最初のデモ可能点。
