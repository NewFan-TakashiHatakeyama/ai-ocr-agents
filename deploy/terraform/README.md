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

## 使い方

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
