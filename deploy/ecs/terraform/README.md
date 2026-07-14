# deploy/ecs/terraform — ECS IaC（ADR-0003 Option A）

Amazon ECS（Fargate 中心・GPUゼロ start）を Terraform で定義する。DD-14／DD-15、
[ADR-0003](../../../docs/adr/0003-deploy-target-ecs.md) に準拠。

## 構成ファイル

| ファイル | 内容 |
|---|---|
| `versions.tf` | terraform / aws provider 版、backend（コメント） |
| `variables.tf` / `terraform.tfvars.example` | 入力（VPC・サブネット・ACM・イメージタグ・DBシークレット） |
| `locals.tf` | サービス一覧（app/inference）と外部公開サービス定義 |
| `cluster.tf` | ECS クラスタ（app / inference）＋ Service Connect 名前空間 |
| `ecr.tf` | 各サービスの ECR リポジトリ＋ライフサイクル |
| `iam.tf` | タスク実行ロール／タスクロール |
| `alb.tf` | ALB＋HTTPS リスナー＋ホストベースルーティング＋WAF（gateway/web のみ公開） |
| `services.tf` | `modules/fargate_service` を for_each で各サービスに適用 |
| `autoscaling_queue.tf` | キュー深度スケール（SQS 採用時のみ・既定オフ） |
| `modules/fargate_service/` | 再利用モジュール（task-def＋service＋Service Connect＋CPU オートスケール） |

## 対象サービス（Option A）

- app クラスタ（Fargate）: gateway, web, orchestrator, ingest, memory, export, llm-adapter
- inference クラスタ（Fargate・OpenVINO CPU）: structure, ocr
- **vl は未配置**（GPU 必須。Option B で `newfan-gpu`（EC2 GPU 容量プロバイダ）に追加）

## 前提（このスタックの外で用意）

- VPC・サブネット（`vpc_id` / `*_subnet_ids` で渡す）
- ACM 証明書、Route53（`api.<domain>` / `app.<domain>` を ALB へ）
- RDS（PostgreSQL）・ElastiCache（or SQS）・EFS（memory-svc の FAISS）… 別スタックで作成し、
  接続情報を Secrets Manager / SSM 経由でタスクへ注入
- ECR への CI push（`image_tag` を合わせる）

## 使い方

```bash
cd deploy/ecs/terraform
cp terraform.tfvars.example terraform.tfvars   # 値を埋める
terraform init
terraform plan
terraform apply
```

> 注意: このリポジトリでは `terraform` 実行環境が無いため `fmt/validate` は未実施。
> 適用前に `terraform fmt -check` と `terraform validate` を CI で通すこと。

## Option B への拡張（VL/GPU 追加時）

1. `newfan-gpu` クラスタ＋ EC2 GPU 容量プロバイダ（g5、ECS GPU 最適化 AMI、ASG）を追加。
2. vl-svc（および必要なら structure/ocr の GPU 版）を EC2 起動タイプの task-def で定義
   （`resourceRequirements` に `{"type":"GPU","value":"1"}`）。
3. 品質ゲート NG ページの VL 有効化（orchestrator の設定）。DD-15 の再検討条件参照。
