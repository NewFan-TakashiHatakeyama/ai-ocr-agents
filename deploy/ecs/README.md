# deploy/ecs — Amazon ECS デプロイ（ADR-0003）

本番プラットフォームは ECS。CPU 系は Fargate、GPU 必須の推論のみ ECS on EC2（GPU）。
詳細は [ADR-0003](../../docs/adr/0003-deploy-target-ecs.md)。

## クラスタ構成

| クラスタ | 起動タイプ | 載せるサービス |
|---|---|---|
| `newfan-app` | Fargate | gateway, web, orchestrator, ingest, memory, export, llm-adapter |
| `newfan-inference` | Fargate（OpenVINO CPU） | structure-svc, ocr-svc（Option A 既定） |
| `newfan-gpu`（Option B のみ） | EC2 GPU（g5, 容量プロバイダ） | vl-svc（＋必要なら structure/ocr GPU 版） |

- 外部公開: gateway / web のみ **ALB + WAF**。
- 内部通信: **ECS Service Connect**（Cloud Map 名前空間 `newfan.internal`）。推論は非公開。
- シークレット: **Secrets Manager**（`secretsArn` 参照）。設定: **SSM Parameter Store** / 環境変数。
- イメージ: **ECR**。

## オートスケール

| サービス | スケール指標 |
|---|---|
| gateway / web | ターゲット追跡 `ALBRequestCountPerTarget` |
| orchestrator / structure / ocr | キュー深度（SQS `ApproximateNumberOfMessagesVisible` or 独自CW）＋ Application Auto Scaling |
| その他 | CPU ターゲット追跡 |

## タスク定義テンプレート

- [`task-definition.app-service.template.json`](task-definition.app-service.template.json)
  — Fargate CPU アプリサービス（gateway/orchestrator 等）の雛形。`${...}` を環境ごとに置換。
- GPU 推論（Option B）は `requiresCompatibilities: ["EC2"]` ＋
  `resourceRequirements: [{"type":"GPU","value":"1"}]` を付け、GPU 容量プロバイダのクラスタへ配置する。

## ローカルとの対応

`deploy/compose.yaml`（1 service = 1 task definition）がローカルプロキシ。ローカルで
compose で通した構成をタスク定義に写像する。structure/ocr は Fargate 相当（CPU）で動くよう、
ローカルでも OpenVINO CPU イメージ／`--device cpu` で確認する。

## 未整備（TODO）

- IaC（Terraform 推奨）で cluster / service / task-def / ALB / WAF / autoscaling / Service Connect を定義。
- ECR リポジトリと CI からの push。
- RDS / ElastiCache（or SQS）/ EFS（memory-svc の FAISS スナップショット）のプロビジョニング。
