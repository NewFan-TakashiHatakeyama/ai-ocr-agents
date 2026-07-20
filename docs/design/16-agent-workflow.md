# §16 エージェントワークフロー（自動化層）詳細設計 v0.2

- 状態: **Approved for implementation**（P1 実装済み。P2 から着手可）
- 日付: 2026-07-21（v0.1: 2026-07-16）
- 親: `NewFan_AI-OCRエージェント詳細設計書_v1.2.md` §16
- 調査: [16-research-report.md](16-research-report.md)（技術検証報告書）/ [16-research-brief.md](16-research-brief.md)（調査依頼書）
- 関連: ADR-0003（ECS/Fargate）、DD-11（抽出グラフ不可侵）、DD-12（DB格納の制約）、DD-13（冪等取込）

## v0.1 からの変更（調査報告と実測を反映）

| # | 変更 | 根拠 |
|---|---|---|
| 1 | 実行エンジンを「1 メッセージ = 1 ノードの継続渡し自前実装」から **LangGraph durable execution** に変更 | 既製エンジンはライセンス（Dify/n8n/Windmill）か常駐要件（Temporal/Prefect/Argo/Kestra）で全滅。既存の LangGraph 1.2.9 + PostgresSaver 3.1.0 が追加コストゼロで要件を満たすことを**ローカル実測で確認**（§16 実測結果） |
| 2 | `source.folder_watch`（60 秒ポーリング常駐）を廃止し、**S3 イベント駆動**（EventBridge → SQS バッファ → trigger consumer）の `source.s3_event` に置換 | 常駐 watcher は「使う時だけ up（月 $28）」運用と両立しない。常駐させると実質 $606/月 に戻る（報告書 Q6） |
| 3 | SCR-07 エディタは **React Flow（MIT）+ pydantic→JSON Schema→rjsf** のフォーム自動生成で確定 | React Flow は商用利用サブスク不要（公式 Discussion #3397）。`catalog()` は P1 実装済み |
| 4 | ワークフロー用 checkpointer を**専用 PG スキーマ `lg_wf`** に分離（DD-11 の生命線） | PostgresSaver はテーブル名固定・スキーマ非修飾で、Python 版に `schema=` 引数が無い（Issue #7345）。`options='-csearch_path=lg_wf'` で分離できることを**実測で確認** |
| 5 | 条件式は MVP 単項のまま、**単一ノード内 AND/OR（混在なし）を post-MVP 拡張として予約** | n8n/Dify とも複合条件を標準提供しており実需がある（報告書 Q8）。文法追加は後方互換 |

---

## 1. 目的とスコープ

### 1.1 これは何か

admin が「どのデータソースを、どのスキーマで抽出し、どう分岐・変換し、どこへ届けるか」を
ノードエディタ（SCR-07）で定義する自動化層。代表例：

> S3 の `invoices/` に帳票が置かれたら → 請求書 v4 で抽出 → 要確認は HITL へ／
> 自動確定分はマッピングして基幹 DB へ UPSERT → Slack 通知

### 1.2 既存資産との境界（最重要・DD-11）

**抽出グラフ（§4）には一切手を入れない。** ワークフローは抽出グラフを
「呼ぶ」だけで、その内部構成・ノード・state を知らない。

| レイヤ | 実体 | checkpointer | 状態の見え方 |
|---|---|---|---|
| 抽出グラフ（§4） | LangGraph 15 ノード | PostgresSaver（**public** スキーマ） | extraction_runs / extraction_fields |
| ワークフロー（§16） | LangGraph（graph_json から動的構築） | PostgresSaver（**lg_wf** スキーマ） | workflow_runs / workflow_node_runs |

両方 LangGraph だが **checkpointer のテーブルをスキーマで完全分離**する。
PostgresSaver は `checkpoints` 等のテーブル名が固定・スキーマ非修飾のため、同居させると
両レイヤの実行状態が同一テーブルに混ざる（thread_id の名前空間では行の分離にしかならない）。
`options='-csearch_path=lg_wf'` を接続文字列に付けることで分離できることは実測済み
（§16）。接続ごとに search_path が固定されるため、Issue #7345 が警告する
プール経由の漏洩は「接続文字列に焼く」ことで回避する（セッション変数を後から
SET する方式は採らない）。

### 1.3 スコープ外

- 抽出グラフの改変（DD-11）
- 任意 SQL の実行（DD-12。プリペアド生成のみ）
- ワークフローからのスキーマ/ルール変更（§5.5 / §5.8.4 の管理画面の責務）
- **即時処理の保証**。環境停止中に置かれたファイルは up 後に処理される（§7）

---

## 2. アーキテクチャ

```mermaid
graph LR
  subgraph AWS 常設（downでも残る）
    S3[(S3)] --> EB[EventBridge]
    EB --> SQ[(SQS<br/>保持14日)]
  end
  subgraph gateway
    A[/workflows API/]
  end
  subgraph orchestrator-svc
    TC[trigger consumer] --> QW[(q.workflow)]
    R[workflow-runner<br/>LangGraph + lg_wf checkpointer]
    E[抽出ワーカー（既存）]
  end
  SQ -->|up中にdrain| TC
  A -->|手動実行/retry| QW
  QW --> R
  R -->|process.extract enqueue| QX[(q.extract)]
  QX --> E
  E -->|完了notify| QW
  R --> SK[sink: DB/Webhook/File/通知]
```

### 2.1 コンポーネント

| 名前 | 置き場所 | 役割 |
|---|---|---|
| `newfan-workflow` | `packages/workflow` | graph_json モデル・lint・条件式・catalog。純ロジック（**P1 実装済み**） |
| workflow-runner | `services/orchestrator`（新規 `workflow_runner.py` ほか） | q.workflow を消費し LangGraph を invoke/resume |
| graph builder | `services/orchestrator`（新規 `workflow_graph.py`） | graph_json → StateGraph 構築（langgraph は既存の optional-dep パターン） |
| trigger consumer | `services/orchestrator`（worker_main に同居） | SQS を購読し、DD-13 判定 → documents 登録 → workflow_runs 発行 |
| sink アダプタ | runner 内 + 既存 export 資産（webhook 署名/SSRF は流用） | DB/Webhook/File/通知 |

**常駐プロセスは増やさない。** trigger consumer は orchestrator-worker に同居し、
環境が up の間だけ動く。停止中のイベントは SQS（保持 14 日）に溜まり、up で自動的に
drain される。v0.1 の source-watcher（ingest-svc 常駐）は廃止。

### 2.2 実行モデル — LangGraph セグメント実行

1 つの workflow_run は **interrupt で区切られたセグメントの列**として実行される。

```
invoke(start) ──セグメント1──▶ interrupt(await_extract) … 抽出完了 notify …
invoke(resume) ──セグメント2──▶ interrupt(await_hitl)   … 人が確定 …
invoke(resume) ──セグメント3──▶ END
```

- セグメント実行中だけ DB 接続とワーカーを使う。待機中はプロセスも接続も持たない
  （状態は lg_wf の checkpoint に永続化。**プロセスを落として別プロセスで resume
  できることを実測済み**。TTL は無いので放置しても消えない）
- v0.1 の「1 メッセージ = 1 ノードの継続渡し自前実装」は廃止。リトライ（LangGraph
  `RetryPolicy`）・並列（superstep）・resume は LangGraph に任せ、
  自前で書くのは「セグメント境界の管理」と「observable projection の更新」だけになる

### 2.3 多重実行の防止

同一 run への並行 invoke は LangGraph が想定しない。runner はセグメント実行前に
`SELECT ... FOR UPDATE SKIP LOCKED` で workflow_runs の行ロックを取り、
取れなければメッセージを再配信に委ねる（§9 のジョブ基盤と同じ）。

---

## 3. データモデル

DDL は v1.2 §16.6 が正で、`workflows` / `workflow_runs` / `connections` /
`source_cursors` は**既に `0001_initial_schema.py` に存在**する。

### 3.1 真実の所在（v0.2 で明確化）

| データ | 役割 |
|---|---|
| lg_wf の checkpoint（LangGraph） | **実行の真実**。resume はここから。中身は不透明（LangGraph の内部形式） |
| `workflow_runs` / `workflow_node_runs` | **観測用の射影**。API/UI/監査/リトライ判断はこちらを見る。runner が各セグメントで更新する |

checkpoint だけだと API から進捗が見えず、projection だけだと resume できない。
両方を持ち、**projection は checkpoint から常に再構成可能**（監査列を除く）とする。

### 3.2 追加 DDL（migration 0003）

```sql
-- ノード単位の実行履歴。POST /workflow-runs/{id}/retry（失敗ノードからの再実行）と
-- workflow_node_duration_seconds（§16.8）の出所。
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
```

RLS は §7.3 準拠（`ENABLE` + **`FORCE`**）。`_RLS_TABLES` への追加を忘れると
そのテーブルだけ所有者バイパスが残る（`db/tests/test_rls_isolation.py` が検知）。
アプリロールへの GRANT は `ALTER DEFAULT PRIVILEGES` で自動付与（設定済み）。

lg_wf スキーマは migration ではなく `scripts/setup_checkpointer.py`（migrate タスク）
が作る： `CREATE SCHEMA lg_wf` → search_path 付き接続で `saver.setup()` →
`newfan_app` へ GRANT。既存の public 側 checkpointer 設定と同じ経路。

### 3.3 WorkflowState（LangGraph の state）

```python
class WorkflowState(TypedDict, total=False):
    workflow_run_id: str
    tenant_id: str
    document_id: str          # トリガー由来
    run_id: str               # process.extract が発行した抽出 Run
    run_status: str           # 'confirmed' / 'needs_review'（抽出完了 notify から）
    fields: dict[str, Any]    # 採点済みフィールドの断面（条件式・マッピングの入力）
    node_outputs: dict[str, dict]   # node_id → 出力（監査は workflow_node_runs に複製）
```

---

## 4. graph_json とノードカタログ

### 4.1 スキーマ

P1 実装済み（`packages/workflow/models.py`）。原則は「**未知のものは保存時に弾く**」
（未知の type・config の typo・不正な条件式・識別子でないテーブル名）。

### 4.2 ノードカタログ（MVP・13 種）

| カテゴリ | type | config（契約） | 実装 Phase |
|---|---|---|---|
| トリガー | `source.s3_event` | `connection_id`, `prefix`, `extensions` | P4 |
| トリガー | `source.manual` | — | P3 |
| トリガー | `source.schedule` | `cron`（5 フィールド） | P8 |
| トリガー | `source.email_attachment` | `connection_id`, `from_filter?`, `subject_filter?` | P8 |
| 処理 | `process.classify` | `doc_types[]`, `on_unknown` | P8 |
| 処理 | `process.extract` | `schema_id`（**必須**）, `options.force_vl` | P3 |
| 分岐 | `branch.condition` | `branches[{when,to}]`, `else`（**必須**） | P3 |
| 分岐 | `branch.hitl_gate` | `priority_boost?`, `assignee_group?`, `sla_hours?` | P5 |
| 変換 | `transform.map_fields` | `mappings[{from\|const, to, format?, mask?}]` | P3 |
| 出力 | `sink.db_write` | `connection_id`, `table`, `mode`, `keys[]`, `on_failure` | P6 |
| 出力 | `sink.webhook` | `connection_id`, `payload_template?` | P3 |
| 出力 | `sink.file` | `connection_id`, `path`, `format` | P6 |
| 出力 | `sink.notify` | `connection_id`, `template`, `when?` | P8 |

v0.1 の `source.folder_watch`（`interval_sec` ポーリング）は**廃止**。
`source.s3_event` はポーリングせず、S3 → EventBridge → SQS の経路で発火する（§7）。

**有効化時の capability 検査**: 未実装 Phase のノードを含むワークフローは activate を
拒否する（保存・lint は通る。「置けるのに動かない」を有効化の境界で止める）。

### 4.3 条件式

P1 実装済み（`packages/workflow/expr.py`）。単項の限定文法：

```
<expr>    := <operand> <op> <literal>
<operand> := run.status | run.confidence | doc.doc_type
           | field['<名前>'].value | field['<名前>'].confidence
```

- 型は parse 時に検査（`run.status > 5` は保存時に落ちる）
- 値が取れない帳票はどの条件にも一致せず else へ落ちる（`!=` でも True にしない）
- `and`/`or` は明示エラー（黙って部分解釈しない）

**post-MVP 拡張（予約）**: 実運用エンジン（n8n/Dify）は複合条件を標準提供しており
実需がある（報告書 Q8）。n8n 方式の「**単一ノード内複数条件、AND または OR、混在なし**」
を拡張候補とする。`branches[].when: str` を `when: str | {all: [str]} | {any: [str]}` に
広げる形で後方互換に足せる（保存済みの単項式はそのまま）。

### 4.4 エディタ（SCR-07）の実現手段【確定】

- **React Flow（xyflow, MIT）**。商用製品での利用にサブスク不要（公式 Discussion #3397）。
  custom node は memo 化を前提とする（DOM 描画のため）
- ノード設定フォームは **pydantic → `model_json_schema()` → rjsf** の自動生成。
  P1 の `catalog()` がこの JSON Schema を返す（13 種を手書きしない）。
  rjsf は `anyOf`/`oneOf` に制約があるため、`transform.map_fields` の
  from/const 排他などは custom widget が要る想定（P7 で PoC）

---

## 5. 構成 lint

P1 実装済み（L001〜L010、`packages/workflow/lint.py`）。規則 ID は UI との契約。
L009（schema 実在）/ L010（connection 実在＋疎通）は参照解決関数の注入で評価。
activate は「error ゼロ + capability 検査 + dry-run 成功（P6 以降）」を条件とする。

---

## 6. 実行エンジン（workflow-runner）

### 6.1 グラフ構築

graph_json → `StateGraph` を**実行時に動的構築**する（`workflow_graph.py`）。

- ノードは種別ごとの実装関数をラップし、開始/終了/attempt を `workflow_node_runs` に記録
- `branch.condition` は `add_conditional_edges` + P1 の `parse_expr`/`evaluate`
- 実測: 代表 6 ノードの構築+compile は **median 0.82ms**（200 回、ローカル）。
  compile キャッシュは不要だが、(workflow_id, version) キーの dict キャッシュを
  入れておく（コスト極小・再検証不要のため）

### 6.2 セグメント実行と interrupt の分類

ノード実装は「待つ」代わりに `interrupt(payload)` する。payload の `kind` が契約：

| kind | 発生ノード | 待つもの | 再開契機 |
|---|---|---|---|
| `await_extract` | `process.extract` | 抽出 Run の完了 | 抽出ワーカーの完了 notify |
| `await_hitl` | `branch.hitl_gate` | 人の確定 | `document.confirmed` notify |

runner は invoke 結果に `__interrupt__` があれば kind を見て
`workflow_runs.status`（`running` / `waiting_hitl`）と `state.waiting` を更新し ack する。

### 6.3 メッセージと再開

`q.workflow`（Redis Stream、§9 準拠）に 2 種類：

```json
{"type": "start",  "tenant_id": "...", "workflow_run_id": "..."}
{"type": "resume", "tenant_id": "...", "workflow_run_id": "...",
 "event": {"kind": "extract_done", "run_id": "...", "status": "needs_review"}}
```

resume は `Command(resume=event)` で invoke する。**抽出ワーカーへの追記は
「ジョブ payload に notify 先が書かれていたら完了時にそこへ積む」の 1 点だけ**
（`{"notify": {"stream": "q.workflow", "workflow_run_id": "..."}}`）。
ワーカーはワークフローの概念を知らないままで済む（DD-11）。
既存の webhook（§6.4、顧客向け・失敗や再送がある）には内部制御を乗せない。

### 6.4 冪等性（実測した再実行境界に基づく）

resume の再実行境界を実測した結果（§16）:

- **完了済みノードは再実行されない**（checkpoint はノード完了ごと）
- **interrupt を含むノードの、interrupt より前のコードは resume のたびに再実行される**

したがって：

| ノード | 規則 |
|---|---|
| `process.extract` | enqueue は interrupt の**前**にあるため再実行される。`Idempotency-Key = workflow_run_id + node_id` で二重 Run を防ぐ（gateway の既存 idempotency 機構を流用） |
| `sink.db_write` | upsert は自然冪等。insert は `ON CONFLICT DO NOTHING` 用の書込み台帳キー（workflow_run_id + node_id + 行番号）を付与（P6） |
| `sink.webhook` / `notify` | at-least-once。イベント id（workflow_run_id + node_id）を payload に含め、受信側で dedupe 可能にする |
| `transform.map_fields` / `branch.*` | 純関数。無条件に安全 |

### 6.5 リトライと失敗

- ノードの一時故障は LangGraph `RetryPolicy`（既定 3 回・指数）
- 枯渇したら `workflow_node_runs.status='failed'` → sink は `on_failure` ポリシー
  （`retry / skip_and_notify / halt_notify`、既定 `halt_notify`）→ run は `failed`
- `POST /workflow-runs/{id}/retry` は失敗セグメントの再 invoke（checkpoint から再開。
  完了済みノードは再実行されないことが実測で保証されている）
- DLQ は §9 準拠（`q.workflow.dead`）

### 6.6 checkpoint の掃除

PostgresSaver に TTL は無い（放置しても消えない — 実測で 3 日前へ backdate した
checkpoint からの resume が成功）。終端（succeeded/failed/skipped）から 30 日で
lg_wf の該当 thread を削除するクリーンアップを `up` 時の migrate と週次（稼働中のみ）で
実行する。workflow_runs 自体の保持は §7.4 に従う。

---

## 7. トリガー

### 7.1 `source.manual`（P3）

`POST /workflows/{id}/runs {document_id}`。UI アップロード起点と API 起点の両方が
これに乗る。P3 の E2E はこの経路で行う。

### 7.2 `source.s3_event`（P4）— 常駐なしのイベント駆動

```
S3 (EventBridge通知 ON)
  → EventBridge ルール（prefix/suffix フィルタ）
  → SQS（保持 14 日、DLQ 付き）          ← ここまで down 中も生きている常設・ほぼ $0
  → trigger consumer（orchestrator-worker 同居。up の間だけ購読）
      DD-13: (source_key, ETag) を source_cursors へ INSERT ON CONFLICT → 重複 skip
      新規なら: S3 から取得 → ingest（§5.1）→ documents 登録 → workflow_runs 発行 → q.workflow
```

- **環境停止中のイベントは SQS に溜まり、up すると自動で drain される。**
  「使う時だけ up」の運用（月 $28）と自動トリガーが常駐なしで両立する
- 稼働中の検知遅延は SQS ポーリングの数秒。`watch_lag_seconds` は
  SQS の最古メッセージ滞留時間（ApproximateAgeOfOldestMessage）で測る
- 調査報告の案 2（EventBridge → 直接 RunTask）は採らない。down 中はタスクを
  起動しても DB が無く、イベントごとの Fargate 起動 30-45 秒を毎回払うため
- **「即時処理」は約束しない**。down 中に置かれたファイルの処理は次回 up 時。
  即時性が契約要件になった顧客には常時稼働環境（別料金）を提示する — これは
  プロダクト判断であり本設計はどちらも壊さない

DD-13 の content_hash は S3 ETag を使う（ダウンロード不要）。multipart アップロードの
ETag は MD5 でないが、「同一キー＋同一 ETag なら同一内容」という同一性判定には足りる。

### 7.3 `source.schedule`（P8）

EventBridge Scheduler → SQS（同じ経路に積む）。cron は graph_json 側の定義を
activate 時に Scheduler へ同期する。

### 7.4 運用ノート（RDS）

- 停止済み RDS は**7 日で自動再起動**する（AWS 仕様）。本環境は down = destroy 運用
  なので該当しないが、pause 運用を選ぶ場合は RDS-EVENT-0154 起点の自動再停止が要る
- Aurora Serverless v2 の scale-to-zero（復帰 ~15 秒）は将来の選択肢として記録
  （現行は RDS PostgreSQL 16。移行判断は別 ADR）

---

## 8. HITL ゲート

`branch.hitl_gate` は上流 extract の結果が `needs_review` のときだけ
`interrupt({kind: "await_hitl"})` する（confirmed なら素通り）。
`workflow_runs.status='waiting_hitl'` に遷移し、レビューキューへ priority_boost を反映。

再開は §6.3 の notify と同一機構：既存の確定フロー（gateway `/confirm` → 抽出ワーカーの
resume → finalize）が完了したとき、ワーカーの完了 notify が `q.workflow` へ
`{kind: "confirm_done"}` を積む。**抽出側に新しい概念は増えない。**

### 8.1 auto_confirm（§16.1・変更なし）

- 既定 OFF。`auto_confirm=true` でも critical フィールドを含む帳票は必ず HITL を通す
- 緩和はテナント設定＋警告＋ audit_logs（未決 Q3 のまま。現状
  `critical_exact_match=0.800` で自動確定を許すのは時期尚早）

---

## 9. sink の安全策（DD-12・変更なし、調査で妥当性確認）

- プリペアド生成のみ・INSERT/UPSERT 限定・`allowed_tables` 完全一致・1 run 1,000 行上限
- 識別子は **allowed_tables ホワイトリスト照合を先に**行い、その上で
  `psycopg.sql.Identifier` でクォート（報告書 Q7 の推奨と一致。Identifier だけに頼らない）
- dry-run: sink を実行せず「生成される SQL / ペイロードのプレビュー」を返す。
  **有効化は dry-run 成功が前提**
- 接続情報は `connections.secret_ref`（Secrets Manager 参照）のみ DB に置く。
  n8n の DB 内暗号化（`N8N_ENCRYPTION_KEY`）より我々の方式が厳格（報告書 Q7）

---

## 10. 接続管理

- `connections.config` に秘密を入れない。`secret_ref` の実体は利用者自身が
  Secrets Manager に登録し ARN を渡す（LLM キーと同じ運用）
- 既存 `POST /webhooks/endpoints` は connections(type=webhook) へ書いており、§16 の
  接続管理と**既に同一テーブル**。残作業は `config.secret` → `secret_ref` への移行のみ
  （P6。旧 Q5 はこれで実質解消）

---

## 11. API（§16.7 + v0.2 追加）

| メソッド | パス | 概要 | 権限 | Phase |
|---|---|---|---|---|
| GET / POST | `/workflows` | 一覧／新規作成（draft v1） | admin | P2 |
| GET / PUT | `/workflows/{id}` | 取得／更新（**新版作成**） | admin | P2 |
| POST | `/workflows/{id}/activate` `/pause` | 有効化（lint error ゼロ + capability + dry-run）／停止 | admin | P2 |
| POST | `/workflows/{id}/lint` | 保存せず lint 結果を返す（エディタ用） | admin | P2 |
| GET | `/workflows/catalog` | ノード種別 → config JSON Schema（rjsf フォーム生成用） | admin | P2 |
| POST | `/workflows/{id}/runs` | **手動実行**（`{document_id}`） | uploader+ | P3 |
| GET | `/workflows/{id}/runs` | 実行履歴（status／期間） | viewer+ | P3 |
| GET | `/workflow-runs/{id}` | run 詳細（node_runs 込み） | viewer+ | P3 |
| POST | `/workflow-runs/{id}/retry` | 失敗セグメントからの再実行 | admin | P3 |
| POST | `/workflows/{id}/dry-run` | ドライラン（sink プレビュー） | admin | P6 |
| GET / POST | `/connections`・POST `/connections/{id}/test` | 接続管理／疎通テスト | admin | P4/P6 |

版の固定（§16.1）: 有効化＝版の固定。`workflow_runs.workflow_version` が開始時点の版を
指し続ける。

---

## 12. 可観測性（§16.8）

`packages/metrics` に追加（名前が割れないよう既存 §12.1 と同じ場所で定義）：

| メトリクス | 型 | ラベル | 出所 |
|---|---|---|---|
| `workflow_runs_total` | counter | `workflow`, `status` | runner の終端遷移 |
| `workflow_node_duration_seconds` | histogram | `type` | workflow_node_runs |
| `sink_write_rows_total` | counter | `connection` | db_write（P6） |
| `watch_lag_seconds` | gauge | `connection` | SQS ApproximateAgeOfOldestMessage |

アラート: 同一ワークフロー failed 連続 5 / watch_lag > 10 分（稼働中のみ）/
sink 失敗率 > 5%/1h。workflow_runs を Langfuse trace に紐付け、抽出 Run の trace へリンク。

---

## 13. セキュリティ

- 業務テーブル（workflow_node_runs 含む）は RLS `ENABLE`+`FORCE`。runner / trigger
  consumer も**アプリロール**（`newfan_app`）で接続する — 所有者で繋ぐと RLS が
  効かない（実 RDS で実測済みの罠）。PostgresSaver が非所有ロールで動くことは実測済み
- **lg_wf の checkpoint テーブルは RLS 対象外**（tenant_id 列が無い LangGraph 内部
  スキーマ）。防御は (a) バックエンドのみアクセス・API 非公開、(b) thread_id =
  workflow_run_id（テナント接頭辞付き ID）、(c) newfan_app への GRANT のみ。
  これは既存の抽出側 checkpointer（public）と同じ姿勢
- config 変更・activate/pause・dry-run・retry は audit_logs（actor=human/agent）
- sink 宛先は SSRF ガード（`newfan-netguard`）と allowed_tables で制限

---

## 14. 段階実装計画（v0.2）

具体的なファイル・完了条件は [16-implementation-plan.md](16-implementation-plan.md)。

| Phase | 内容 | 完了条件（実測ベース） |
|---|---|---|
| **P1 ✅** | `packages/workflow`（モデル+lint+条件式+catalog） | 68 テスト緑（完了） |
| **P2** | workflows CRUD / activate / lint / catalog API + migration 0003 | 実 AWS で保存→lint→有効化。実 PG の RLS テスト緑 |
| **P3** | runner（LangGraph）+ manual 実行 + extract/condition/map/webhook | **実 AWS で manual → 抽出 → 分岐 → webhook が端から端まで動く**。プロセス kill → resume 継続を実 AWS で確認 |
| **P4** | S3 イベント駆動（EventBridge→SQS→consumer）+ DD-13 + terraform | S3 に置いた帳票が自動処理。同一ファイル再配置が skip。**down 中に置く → up → 自動 drain** を実測 |
| **P5** | HITL ゲート | needs_review → 人が確定 → 下流継続を実系で確認 |
| **P6** | sink.db_write（DD-12）+ dry-run + connections test + secret_ref 移行 | dry-run プレビュー = 実 SQL。allowed_tables 外・行数超過の拒否をテストで固定 |
| **P7** | SCR-07（React Flow + rjsf） | catalog 由来のフォームで 13 種の config を編集し保存→lint 往復 |
| **P8** | schedule / classify / notify / email / checkpoint 掃除 / §16.8 メトリクス | 各機能の実測確認 |

---

## 15. テスト戦略

- lint / expr / モデル: 純関数の単体（P1 済・68 件）
- **runner は実 PostgreSQL で結合**（`DATABASE_URL_TEST` ゲート、既存 db/tests と同形式）。
  検証済みプローブ（`scripts/probe_langgraph_workflow.py`）を P3 でテスト化する:
  スキーマ分離 / 非所有ロール / プロセス跨ぎ resume / 再実行境界
- DD-13 は実 DB の UNIQUE に当てる（アプリ側判定では同時実行で抜ける）
- db_write は実 PG に対して（Identifier クォート・allowed_tables 完全一致・行数上限）
- ワークフロー結合テストでは**抽出グラフを Fake にしてよい**（DD-11 の境界。
  抽出の精度はゴールデンセット §14.2 の担当）

---

## 16. 実測結果（2026-07-21、ローカル PG 16 / langgraph 1.2.9 / checkpoint-postgres 3.1.0）

調査報告書が「Web 調査では検証不能。ローカル実測必須」とした 4 項目を実測した。
再現スクリプト: `scripts/probe_langgraph_workflow.py`。

| # | 項目 | 結果 |
|---|---|---|
| 1 | checkpointer のスキーマ分離 | **OK**。`options='-csearch_path=lg_wf'` で 4 テーブルが lg_wf に作成され、public（抽出側）と完全分離。lg_wf に置いた thread は public の saver から不可視 |
| 2 | 非所有ロールでの PostgresSaver | **OK**。`newfan_app`（rolsuper=F, rolbypassrls=F）で put/get 成功（GRANT のみ） |
| 3 | プロセス跨ぎ resume | **OK**。interrupt → プロセス終了 → **別プロセス**で `Command(resume=...)` → 完走。checkpoint の ts を 3 日前に書き換えても resume 成功（TTL なしの傍証） |
| 3' | **再実行境界**（冪等性設計の根拠） | 完了済みノードは再実行**されない**（extract 実行回数 1 のまま）。interrupt を含むノードの interrupt **より前**のコードは resume で再実行**される**（実行回数 1→2）。§6.4 の冪等規則はこの実測に基づく |
| 4 | graph_json からの動的構築 + compile | **median 0.82ms / mean 0.92ms / max 13.8ms**（200 回）。実行時構築で問題ない |

未実測（P4/P7 で実施）: S3→EventBridge→SQS の end-to-end 遅延（実 AWS）、
rjsf の 13 種フォーム生成、React Flow の描画性能。

---

## 17. 未決事項の現況

| # | 論点 | 現況 |
|---|---|---|
| Q1 | MVP トリガー範囲 | **解決**: manual（P3）→ s3_event（P4）→ schedule/email（P8）。常駐 watcher は廃止 |
| Q2 | 条件式 and/or | **方針決定**: MVP 単項のまま。post-MVP で単一ノード内 AND/OR（混在なし）を後方互換で追加（§4.3） |
| Q3 | critical の自動確定 | **未決のまま**（プロダクト判断）。現状 critical_exact_match=0.800 で緩和は時期尚早 |
| Q4 | watcher 常駐と運用の両立 | **解決**: EventBridge→SQS バッファで常駐なし。即時処理は約束しない。即時性が要る顧客は常時稼働（別料金）をプロダクト側で切り出し |
| Q5 | webhooks/endpoints と connections | **実質解決**: 既に同一テーブル。secret→secret_ref 移行のみ P6 に残る |
| Q6 | SCR-07 の実装方式 | **解決**: React Flow（MIT）+ rjsf。PoC は P7 冒頭 |
