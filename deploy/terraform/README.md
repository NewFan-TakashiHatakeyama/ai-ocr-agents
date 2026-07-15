# deploy/terraform — AI-OCR の AWS 基盤（IaC, ADR-0003）

Fargate 上に MVP 一式を載せる IaC。**このリポジトリで唯一の正の Terraform スタック**
（かつて `deploy/ecs/terraform` にもう 1 つあったが、実在しないサービス構成を前提にしていたため削除）。

作るもの:

- **VPC**（AI-OCR 専用）: public/private subnet × 2AZ、NAT Gateway 1 台、S3 Gateway エンドポイント
- **RDS PostgreSQL 16**（正本 DB, §7）と **ElastiCache Redis 7**（キュー, §9）
  ＋ それぞれの接続文字列を Secrets Manager に格納
- **ECR**（gateway / orchestrator-worker / export-worker / migrate / inference）
- **ECS クラスタ**（Service Connect 名前空間つき）と各サービス
  （gateway・orchestrator-worker・export-worker・**structure-svc**・**ocr-svc**）、migrate タスク定義
- **ALB**（gateway のみ外部公開）、IAM、CloudWatch Logs

## 命名とタグ

同一アカウントに **GraphSuite**（`graphsuite-api` / `graphsuite-dev`）や **AIReadyConnect**(PoC)
が同居する。どれが AI-OCR のリソースか一目で分かるよう、

- 名前は全て **`ai-ocr-` 接頭辞**（`var.name_prefix`。`ai-ocr` を含まない値は validation で弾く）
- タグは provider の `default_tags` で全リソースに `Service=ai-ocr` / `Project=newfan-ai-ocr` /
  `Env` / `ManagedBy=terraform` / `Repo=ai-ocr-agents` を付与。加えて各リソースに `Name` タグ

を徹底する（既存の GraphSuite リソースはタグ無しで運用されているため、
コスト配分・棚卸しは `Service=ai-ocr` で引ける状態にしておく）。

## 前提（このモジュールでは作らない = 変数入力）

- **Secrets**: `jwt_secret_arn` / `anthropic_secret_arn`（任意で `gemini_secret_arn`）。
  DB と Redis の URL は本スタックが作成するため渡さない。
- **S3**: `export_s3_bucket`（＋任意 `export_s3_kms_key_id`）。
- **ACM 証明書**: `acm_certificate_arn` を渡すと ALB に HTTPS:443 リスナを追加（空なら HTTP のみ）。

## 起動 / 停止とコスト（月に数回しか使わない前提）

立てっぱなしは **$606.69/月（約91,000円）**。うち推論 2 台の 24/7 が $270（44%）を占めるが、
月 1 万枚の処理に実際に必要な推論時間は **46 時間（1 台の 6.2%）** しかない。
**使う時だけ立てる運用**が前提。

止め方は 2 段階ある。AWS の仕様上、**ElastiCache / NAT Gateway / ALB には「停止」が無く削除しかない**
ため、月単位で使わないなら `down`（destroy）が正解になる。

| 操作 | 何が起きる | 止めても残る額 | 復旧時間 |
|---|---|---|---|
| `pause` | Fargate を 0 台に。DB とエンドポイントは保持 | **約 $189/月** | 数十秒 |
| `down` | スナップショットを取って destroy | **約 $3.95/月**（ECR＋snapshot） | 15–20 分 |

`pause` は「今夜は使わない」用。RDS も停止すれば $115/月まで下がるが、
**RDS の停止は 7 日で自動起動する**ため月数回の運用には使えない。

```bash
scripts/aws_env.sh cost     # 時間単価と概算
scripts/aws_env.sh up       # 起動（apply）
scripts/aws_env.sh status   # 今どれが課金されているか
scripts/aws_env.sh down     # スナップショット取得 → destroy
```

起動中の実費は **$0.766/h（約114円/時）**＝ Fargate $0.529 + RDS/Redis/NAT/ALB $0.236。

| 使い方 | 月額 |
|---|---|
| 月2回 × 4時間 | 約 $10（約1,500円） |
| **月4回 × 8時間** | **約 $28（約4,300円）** |
| 月8回 × 8時間 | 約 $53（約7,900円） |
| 立てっぱなし | $606.69（約91,000円） |

単価は AWS Pricing API から取得した ap-northeast-1 の実値。処理時間は実測（16.4秒/枚 @4vCPU）。

開発環境では `container_insights_enabled=false`（$18/月の削減）を推奨。
さらに削るなら推論を Fargate Spot に載せる（約7割引）、NAT を捨てて public subnet に置く、等。

> **`down` はデータを消す。** スクリプトが destroy 前に手動スナップショットを取るが、
> **復元は手作業**（スナップショットから RDS を作成し直す）。terraform の `snapshot_identifier` は
> マスタパスワードの扱いが分かりにくく、復元した DB と `DATABASE_URL` が食い違って接続不能に
> なりうるため、あえて自動化していない。

## 使い方（素の terraform）

```bash
cd deploy/terraform
terraform init
terraform validate
terraform plan  -var-file=env/production.tfvars
terraform apply -var-file=env/production.tfvars
```

デプロイ順: CD で ECR に push → `terraform apply` → migrate を単発実行 → サービスが新イメージで起動。

```bash
aws ecs run-task --cluster $(terraform output -raw cluster_name) \
  --task-definition $(terraform output -raw migrate_task_definition_arn) \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[...private...],securityGroups=[$(terraform output -raw service_security_group_id)]}"
```

## 推論サービング

`structure-svc` / `ocr-svc` は同一イメージ（`ai-ocr-inference`, 4.8GB・モデル焼き込み済み）を共有し、
`PIPELINE_CONFIG` だけが違う。orchestrator-worker は **Service Connect** で
`http://structure-svc:8080` / `http://ocr-svc:8080` として解決する（`local.structure_url`）。

- 実測（sample2.png, warm, paddlepaddle 3.2.2）: 4 vCPU=16.4s/枚、8 vCPU=11.6s/枚
- 印章オプション: `structure_seal_enabled=true` で印章版 config に切り替わる（+2% 程度）
- `var.structure_cpu` / `var.structure_memory` で増強できる（Fargate の有効な組み合わせに従うこと）

## スコープ外（次の IaC 増分）

- **WAF** の ALB アタッチ、**Application Auto Scaling**（gateway=ALBRequestCount、worker=キュー深度）。
- RDS Multi-AZ / Redis レプリカ（`db_multi_az` / `num_cache_clusters` を本番昇格時に見直す）。
- CD からの `terraform apply` と ECS サービス更新の自動化。
- VL（PaddleOCR-VL）は GPU 必須のため Option A では未配置。

## 検証状況

`terraform validate` と、実 AWS（654654601240 / ap-northeast-1）に対する **`terraform plan` を実行済み**
（69 リソース追加の計画が通ることを確認。apply は未実行）。

RDS の `engine_version` はメジャー指定（`16`）にしている。ap-northeast-1 の 16 系は 16.9 以降しか
提供されておらず、マイナーを固定すると存在しない版を掴んで apply が落ちるため（実測で確認）。
