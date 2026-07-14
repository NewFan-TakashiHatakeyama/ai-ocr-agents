# deploy/terraform — ECS デプロイ基盤（IaC, ADR-0003）

Fargate 上に MVP を載せるための最小 IaC。ECR / IAM / CloudWatch Logs / ECS クラスタ /
ALB / サービス（gateway・orchestrator-worker・export-worker）と migrate タスク定義を作る。

## 前提（このモジュールでは作らない = 変数入力）

- **VPC / subnet**: 既存を `vpc_id` / `public_subnet_ids` / `private_subnet_ids` で渡す。
- **RDS(PostgreSQL) / ElastiCache(Redis)**: 別管理。接続文字列を Secrets Manager に置き
  `db_secret_arn` / `redis_secret_arn` で渡す（`DATABASE_URL` は `postgresql+psycopg://...`）。
- **Secrets**: `jwt_secret_arn` / `anthropic_secret_arn`、`export_s3_bucket`（＋任意 KMS）。
- **ACM 証明書**: `acm_certificate_arn` を渡すと ALB に HTTPS:443 リスナを追加（空なら HTTP のみ）。

## 使い方

```bash
cd deploy/terraform
terraform init
terraform validate          # ローカルでは未実行（terraform 未導入環境で作成）。必ず流すこと
terraform plan  -var-file=env/production.tfvars
terraform apply -var-file=env/production.tfvars
```

デプロイ順: CD で ECR に push → `terraform apply` → migrate を単発実行 → サービスが新イメージで起動。

```bash
aws ecs run-task --cluster newfan-app \
  --task-definition $(terraform output -raw migrate_task_definition_arn) \
  --launch-type FARGATE --network-configuration '{...private subnets...}'
```

## スコープ外（次の IaC 増分）

- VPC / NAT / subnet、RDS・ElastiCache のプロビジョニング。
- **WAF** の ALB アタッチ、**Application Auto Scaling**（gateway=ALBRequestCount、worker=キュー深度）。
- **ECS Service Connect**（`newfan.internal` 名前空間、structure/ocr との内部通信）。
- 推論クラスタ（structure-svc / ocr-svc、Option A は Fargate CPU / Option B は GPU EC2）。
- CD からの `terraform apply` と ECS サービス更新の自動化。

## 注意

このモジュールは **`terraform validate` をローカルで未実行**（terraform 未導入環境で作成）。
HCL 構文は python-hcl2 でパース確認済みだが、apply 前に必ず `terraform validate` /
`plan` を実行すること。
