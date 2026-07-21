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

## P4: S3 イベント駆動トリガー + DD-13 【実装済み】

### 変更ファイル（実装実体）

| ファイル | 内容 |
|---|---|
| `deploy/terraform/ecr/trigger.tf`（新規） | **長命スタック側**: inbox バケット `ai-ocr-inbox-<account>` + EventBridge 通知/ルール + SQS `ai-ocr-workflow-trigger`（保持 14 日）+ DLQ（maxReceiveCount=5）。down（destroy）で消えるとイベントを取りこぼすため ecr/ と同居。inbox が長命であることが「down 中も顧客が置ける」前提そのもの |
| `deploy/terraform/workflow_trigger.tf`（新規） | 本体スタック側: SQS/inbox の data 参照 + タスクロールへ sqs:ReceiveMessage 等と inbox s3:GetObject |
| `deploy/terraform/ecs.tf` | orchestrator env に S3_BUCKET / S3_KMS_KEY_ID / TRIGGER_SQS_URL |
| `services/orchestrator/.../workflow_trigger.py`（新規） | S3TriggerConsumer（5 秒間隔ゲートで SQS ポーリング）+ match_s3_event 純関数 + TriggerStore Protocol。キー規約「最上位フォルダ=テナント ID」で RLS 文脈を決定。prefix/extensions はテナントフォルダを除いた相対キーで照合。失敗メッセージは delete しない（再配信→DLQ） |
| `services/orchestrator/.../workflow_store.py` | PgTriggerStore 追加。claim（source_cursors ON CONFLICT）+ documents + pages + workflow_runs を**同一 TX**。ingest は TX 前（再配信時の S3 上書きは無害）、start enqueue はコミット後 |
| `services/orchestrator/.../worker_main.py` | TRIGGER_SQS_URL 設定時のみ consumer を配線（boto3 + IngestService）。同一プロセスの run ループに追加 |
| `services/gateway/.../workflows_repo.py` | IMPLEMENTED_NODE_TYPES に source.s3_event 追加（activate 可能に） |
| `scripts/aws_env.sh` | `status` に SQS 滞留数表示（drain の見える化。ecr スタック未適用なら黙って省略） |
| `scripts/seed_s3_connection.py`（新規） | connections(type='s3', config.bucket=inbox) の seed（migrate run-task 内で実行） |

### DoD（2026-07-21 実 AWS で実測済み・全達成）

- 稼働中: inbox `ten_1/invoices/sample2.png` 配置 → **2 秒で自動取込** → 抽出 →
  webhook 配信（wfrun_77c6d4bc…, total_amount=7003）
- 同一ファイル再配置 → skip（`取込済みのため skip (etag=8dbc5f49…)`。run は増えない）
- **down 中に配置 → SQS 滞留（status コマンドで「待機中: 1」）→ up → 86 秒で drain**
  → run 生成 → 55 秒で succeeded → webhook 配信（wfrun_3195e04c…, total_amount=136998）

E2E 中に DD-02 char_backfill の latent bug を検出・修正した: /ocr の word box が
4 点ポリゴン形式 `[[x,y],…]` で返ると `len(b)==4` ガードを素通りして
`int([x,y])` が TypeError → ジョブが ACK されず永久再配信。矩形/ポリゴン両形を
外接矩形へ正規化して解消（`_word_box_rect`）。滞留していたジョブは修正版デプロイ後に
XAUTOCLAIM で回収され、checkpoint から自動回復して完走した（durable execution の実証）。

規模: 中。

---

## P5: HITL ゲート 【実装済み】

- `branch.hitl_gate` ノード実装（needs_review のときのみ `interrupt({kind:"await_hitl"})`、
  confirmed は素通り）。config（priority_boost/assignee_group/sla_hours）は interrupt
  payload に載り、runner が workflow_runs.state 内の waiting として永続化 →
  `/review/queue` が priority へ加点する
- **計画の「新規配線はノード実装のみ」は誤りだった**: gateway confirm の再開ジョブに
  notify が載っておらず（`extraction_runs.options.workflow_notify` は読み手ゼロの
  デッドデータ）、confirm_done は一度も発火しない状態だった。confirm →
  `OrchestratorClient.resume(..., notify=...)` の中継を実装（ports/prod/routers）
- 敵対的レビューで critical 2 + minor 3 を検出し、critical と回復系を修正:
  1. **extract_done の再配信 1 回で人手ゲートが素通りする**（waiting_hitl 中の再配信が
     pending interrupt の戻り値に注入され、未確定値で webhook 発火 → 本物の
     confirm_done は破棄）→ runner が await_hitl 待ちには confirm_done 以外を破棄
  2. **`/review/queue` が Pg で常に 500**（存在しない waiting 列を参照。waiting は
     state JSONB 内）→ `state->'waiting'->>'priority_boost'` へ修正 + 実 PG テスト
  3. 終端済み run への resume / 完走済み checkpoint への resume が NotReady で
     永久再配信（poison message）→ ack して破棄 / 射影を終端へ同期
- 残課題（minor、P8 で扱う）: confirm の Idempotency-Key 必須化 or worker の
  run 単位ロック（並行 worker での finalize 二重実行）/ ワークフローが待つ run と
  別 run を confirm した場合の waiting_hitl 残留検知（§12 監視）

### DoD（2026-07-21 実 AWS で実測済み・達成）

- S3 `ten_1/hitl/sample2.png` 配置 → 5 秒で run 開始 → 抽出 needs_review →
  **waiting_hitl で停止**（webhook 発火なし）
- `/review/queue` で該当帳票が **priority 25.0（boost 加点）で先頭**
- `POST /documents/{id}/confirm` → **1.8 秒でワークフロー succeeded** →
  webhook 配信 `{"amount": "7003", "source_system": "ai-ocr-hitl"}`（map_fields 経由の確定値）

規模: 小〜中の想定だったが、notify 配線の欠落とレビュー検出バグで中。

---

## P6: sink.db_write（DD-12）+ dry-run + connections 【実装済み】

### 実装実体

| ファイル | 内容 |
|---|---|
| `packages/workflow/dbsink.py`（新規） | プリペアド INSERT/UPSERT 生成の**単一実装**（dry-run と実行が同じ関数 = 「プレビュー = 実 SQL」を構造で保証）。allowed_tables 完全一致 → 識別子検証 → 生成。行数上限 1,000。insert は台帳キー `nf_write_key` + **`ON CONFLICT (nf_write_key) DO NOTHING`**（対象限定。無指定だと業務キー重複まで黙殺され無音データ欠落 — レビューで実 PG 再現）。build_dsn は user/password を percent-encode（記号入りパスワードで実測） |
| `services/orchestrator/workflow_sinks.py`（新規） | PgDbWriter（全行 1 TX・都度接続） |
| `services/orchestrator/workflow_graph.py` | _make_db_write。**map_fields の出力のみ書く（fields への fallback 禁止**＝dry-run 未プレビュー列・mask 迂回値を顧客 DB に書かない）。on_failure: halt_notify（既定・失敗）/ skip_and_notify（下流継続）/ retry |
| `services/orchestrator/aws_secrets.py`（新規） | SecretsManagerResolver（キャッシュ付き実行時 GetSecretValue） |
| `services/gateway/dryrun.py`（新規）+ routers | POST /workflows/{id}/dry-run。**db_write 直前の map_fields はちょうど 1 つ**（分岐合流で列が実行時に変わる乖離をレビューで実証→制限）。db_write を含むグラフの activate は dry-run 成功が前提 |
| gateway 接続管理 | GET/POST /v1/connections + POST /v1/connections/{id}/test（SELECT 1。成功で status='tested'）。config の秘密は**再帰スキャン+部分一致**で拒否、GET は再帰マスク。**secret_ref は `.../conn/<tenant_id>/` 名前空間を強制**（クロステナント秘密窃取をレビューで実証→遮断） |
| webhook secret_ref 移行 | 署名鍵を Secrets Manager（`ai-ocr/<env>/conn/<tenant>/webhook-*`）に保存し DB は secret_ref のみ。旧 config.secret 行は読み出し fallback（resolver 未配線で secret_ref 行を読むと**明示エラー**＝空鍵署名の無音配信を防ぐ） |
| `deploy/terraform/main.tf` | task ロールへ `ai-ocr/<env>/conn/*` 限定の Secrets Manager 権限（従来は実行時 GetSecretValue 不可だった） |
| `scripts/aws_env.sh sink-demo` + `scripts/setup_sink_demo.py` | 「顧客基幹 DB」役（erp_demo スキーマ + INSERT/UPDATE 限定ロール erp_sink）を migrate タスクで用意 |

### DoD（2026-07-21 実 AWS で実測済み・達成）

- POST /connections（secret_ref=`ai-ocr/production/conn/ten_1/erp-demo`、**記号入りパスワード `@ % /`**）→ /test で **SELECT 1 成功 → tested**（実 Secrets Manager 解決 + percent-encode を本番実証）
- dry-run が実 SQL（`INSERT ... ON CONFLICT ("invoice_no") DO UPDATE ...`）をプレビュー → activate 通過
- S3 `ten_1/db/sample2.png` → 7 秒で waiting_hitl → confirm → **1 秒未満で erp_demo.invoices へ UPSERT**: `('GS0001', 'わくわく物産…', '7003', 'ai-ocr')`
- allowed_tables 外の拒否・行数超過の拒否・業務キー重複の可視エラー・台帳キー冪等は実 PG テストで固定

敵対的レビュー（find 4 観点 → 反証 verify）で **major 5 件を出荷前に検出・修正**（全て回帰テスト化）:
無指定 ON CONFLICT の黙殺 / dry-run と実行の列乖離 / secret_ref クロステナント /
config 秘密のネスト回避 / DSN percent-encode 欠如（+エラー応答の秘密断片漏れ）。

規模: 大（安全策が厚い。想定どおり）。

---

## P7: SCR-07 ノードエディタ 【実装済み】

- `web/app/workflows/`: 一覧（作成→遷移）+ エディタ。@xyflow/react（キャンバス・
  カテゴリ色カスタムノード・ドラッグ接続・pos 永続化）+ @rjsf（catalog の JSON Schema
  から 13 種の config フォームを自動生成。配列・ネスト・enum・anyOf を実機確認）
- branch.condition の分岐は config が正（破線ラベル表示のみ。辺として編集させない, §4.1）
- 保存（常に可）→ 自動 lint → 指摘の node_id ハイライト → activatable で有効化活性 →
  dry-run プレビュー表示。未実装ノードはパレットにバッジ
- rjsf の from/const 排他（model_validator）は JSON Schema に現れないため UI では両方
  表示され、保存時にサーバ側で検証される（PoC 論点の結論。custom widget は post-MVP）

### DoD（2026-07-21 実機確認・達成）

- 実 Chrome: ノード追加 → ドラッグ接続 3 本 → map_fields（rjsf 配列）編集 →
  webhook connection_id 入力 → 保存 v2・lint 0 件 → **有効化（active）** まで実操作
- 実 AWS（v0.5.8）: デプロイ済み web の SCR-07 で active ワークフローのグラフ描画・
  catalog 取得・パレット表示を確認
- 注意（自動テスト環境の知見）: プレビューペインの合成ドラッグは d3-drag に届かない。
  ドラッグ系の UI 検証は実 Chrome（Claude in Chrome）で行うこと

規模: 大（UI）。

## P8: 残り 【実装済み】

- **source.schedule**: consumer 内の分ティック + source_cursors dedup
  （workflow+node+分時刻の UNIQUE）。**設計 §7.3 の「EventBridge Scheduler 同期」から
  方式変更**（activate/pause ごとの AWS リソース増減は down 運用と噛み合わない。
  設計書に反映済み）。cron は自前実装（packages/workflow/cron.py。Vixie 規則）、
  **JST 固定評価**。down 中は発火しない（§1.3）。直近 5 分の遡り評価あり
- **process.classify**: 抽出結果 doc_type の許可リスト照合（DD-11: 再分類しない）。
  on_unknown=halt / default_route
- **sink.file**: S3（type='s3' 接続）へ JSON / CSV（BOM 付き）。パスはプレースホルダのみ。
  同一キー上書き＝冪等
- **sink.notify**: Slack incoming webhook 互換（{"text"} POST・SSRF ガード・when 式・
  テンプレート変数）。接続は connections(type='webhook') を流用
- **watch_lag_seconds{connection}**: SQS ApproximateAgeOfOldestMessage（1 分毎）
- **checkpoint 掃除（§6.6）**: 終端 30 日超の lg_wf thread を migrate（up 時）で削除
  （scripts/cleanup_checkpoints.py。常時稼働に移行したら週次を追加）
- **post-MVP へ明示送り**: source.email_attachment（SES 受信→S3 保存で s3_event が代替）、
  Langfuse 紐付け（キー運用未定）、アラート 3 種（Prometheus サーバ未導入のため。
  /metrics は公開済みで、導入時に §12 の式をそのまま張れる）

### DoD（2026-07-21 実 AWS で実測・達成）

- schedule: cron `*/2 * * * *` を activate → **07:24:00.2 に分ちょうどで発火**
  （document 無し run）→ 1.4 秒で succeeded
- notify: webhook.site が 07:24:02 に Slack 互換 `{"text": "⏰ 定期実行: run=wfrun_e0f6…"}`
  を受信（テンプレート展開込み）
- classify / file は InMemory + 実 PG テストで固定（graph 配線・halt/継続・BOM CSV・
  パス展開・接続不在失敗）

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
