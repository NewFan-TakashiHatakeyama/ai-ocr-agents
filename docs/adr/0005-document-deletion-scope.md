# ADR-0005: 帳票削除は物理削除とし、到達範囲を当社管理ストレージに限る

- 状態: Accepted
- 日付: 2026-08-21

## 背景

取り込んだ帳票を消す手段が一切なかった。`DELETE FROM documents` はテストと
`scripts/e2e_real.py` にしか存在せず、UI からも API からも消せない。デモや検証で
取り込んだ帳票が溜まり続け、一覧が実運用に耐えなくなる。

一方で DDL 上、削除は素直な操作ではない。`documents` を FK で参照して
`ON DELETE CASCADE` が効くのは `pages` と `extraction_runs`（および後者経由の
`extraction_fields` / `extraction_tables`）だけで、`correction_logs`・`jobs`・
`workflow_runs` は `documents` を **TEXT 列で指すだけ**（0001 の :154 / :206 / :265）。
DB は何もしてくれない。

## 決定

### 1. 物理削除にする（論理削除・ゴミ箱・Undo は作らない）

論理削除は `documents.status` の CHECK 制約（0001:77-79）の張り替えを要求し、
さらに全一覧クエリにフィルタを撒く必要がある。1 箇所でも漏れると
「消したのに出る」という、削除機能として最悪の壊れ方をする。

用途は 1 件ずつの片付けであり、復元需要は確認ダイアログで足りると判断した。

### 2. 順序は S3 → DB

逆順（DB 先）だと、S3 の削除に失敗したときに `storage_uri` を失って孤児
オブジェクトを辿る手段が消える。この順なら失敗しても DB は無傷で、帳票は一覧に
残ったまま再試行できる（fail-closed）。

### 3. 監査行は削除本体と同じトランザクションで書く

`audit_logs` はアプリロールから DELETE できない（`scripts/ensure_app_role.py`）。
削除の唯一の恒久証跡なので、別トランザクションの best-effort にすると
「消えたのに痕跡が無い」が成立し得る。`detail` に `storage_uri` / `run_ids` /
各表の削除件数を残し、事後の照会に耐えるようにした。

### 4. 実行中は拒否する。ただし時間で見切る

`documents.status IN ('queued','processing','in_review')` と
`extraction_runs.status='processing'` を拒否条件にする。前者が要るのは、
`confirm` が `documents` だけ `in_review` にして `extraction_runs` は
`needs_review` のまま残すため。run の status だけ見ると確定処理の最中に削除が通り、
直後に resume したワーカーが孤児の `correction_logs` を作って webhook を外部へ飛ばす。

ただし `mark_run_failed` はワーカーの例外ハンドラ経由でしか呼ばれず、Fargate の
タスク入れ替えや OOM では `processing` が永久固着する（stale reaper は無い）。
閾値（既定 30 分, `DOCUMENT_DELETE_STALE_MINUTES`）を超えたものは停止済みとみなして通す。
これが無いと「消したい帳票ほど消せない」になる。

`needs_review` は拒否**しない**。レビュー待ちのまま不要と判明した帳票こそ削除需要の中心。

### 5. 必要ロールは reviewer 以上

M2M の API キー（`api` ロール、rank 2）は弾く。外部連携から大量に消せると事故が
青天井になる。一括削除 API も作らない。

## この機能で消えないもの（意図的）

| 対象 | 理由 |
|---|---|
| `audit_logs` | 監査証跡。削除記録そのものがここに残る |
| `workflow_runs` / `workflow_node_runs` の行 | 運用の証跡として残す。ただし `document_id` は NULL 化し、`node_runs` の `input`/`output`（抽出値が生で入る）は NULL 化する。`waiting_hitl` は再開先が消えた以上続行不能なので `failed` に終端化する |
| `tenant_rules` | ルールは複数帳票から derive されるテナント資産。1 帳票の削除で active ルールを退役させると、無関係な帳票の抽出精度が予告なく落ちる |
| `source_cursors` | **消すと害が大きい**。フォルダ監視（gdrive/m365/box）の次回ポーリングで同一ファイルが再取込され、「削除したのに復活し、しかも 2 件になる」。残す代償は、同一ファイルが二度と自動取込されないこと（回避策はファイル差し替えか手動アップロード）。別チケット: `POST /v1/connections/{id}/reclaim` |
| `lg_wf.*` チェックポイント | `workflow_runs` の行を残すので `scripts/cleanup_checkpoints.py` の 30 日 TTL が回収する。削除時に消すと同スクリプトの鍵を自ら壊す |
| inbox バケットの原本 / `sink.file` の顧客バケット出力 | 前者は別スタックでタスクロールが `s3:GetObject` のみ、後者は顧客バケットへ規約外キーで書くため到達不能。監査 `detail` を手掛かりに手動対応 |
| RDS 自動バックアップ / PITR / CloudWatch Logs | 消せない。§7 の運用範囲外 |

## 影響

### この機能は GDPR 消去請求への回答には使えない

到達範囲は**当社管理ストレージの現行データのみ**。RDS のバックアップ・PITR、
CloudWatch Logs（`workflow_trigger.py` は顧客のファイル名をログ出力する）、
顧客バケットへ出力済みの `derived/*.json` は残る。営業資料に
「削除に対応」と書く際は、この線引きを外さないこと。

### `corrections_total` KPI が遡って減る

削除で `correction_logs` を消す以上避けられない。ダッシュボード（SCR-04）の
前月比が動くので、運用に事前告知する。「消えた帳票の分が永久に水増しされていた」
状態の修正でもある。

### ソフトロックは安全境界ではない

`local.desired.gateway = 2` で `InMemoryLockStore` はプロセス内（`locks.py`）。
`locked` 判定はおよそ半分の確率ですり抜ける。他ユーザーの作業を踏まないための
配慮であって、権限の代わりではない。Redis 化は別チケット。

### デプロイ順序

`terraform apply`（IAM に `s3:DeleteObjectVersion` / `s3:ListBucketVersions` を追加）を
**先に**実行する。逆順でもデータ破壊は起きない（`delete_prefix` が `AccessDenied` で
例外 → 500 → DB 無傷）が、機能が全 500 になる。

migration 0005 は FK 1 本（`NOT VALID`）と索引 3 本のみで**新規列は無い**ため、
migrate タスクと gateway サービスの更新順序には依存しない。FK を `VALIDATE` しないのは、
既存の孤児 `correction_logs` を migration が無音で削除するのを防ぐため。
