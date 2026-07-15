# deploy/ecs — Amazon ECS デプロイ（ADR-0003）

本番プラットフォームは ECS。CPU 系は Fargate、GPU 必須の推論のみ ECS on EC2（GPU）。
詳細は [ADR-0003](../../docs/adr/0003-deploy-target-ecs.md)。

## クラスタ構成

| クラスタ | 起動タイプ | 載せるサービス |
|---|---|---|
| `newfan-app` | Fargate | gateway, web, orchestrator, ingest, memory, export, llm-adapter |
| `newfan-inference` | Fargate（CPU, `--engine onnxruntime`） | structure-svc, ocr-svc（Option A 既定） |
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

## タスク定義

- [`task-definition.app-service.template.json`](task-definition.app-service.template.json)
  — 汎用雛形。`${...}` を環境ごとに置換。
- [`task-definition.gateway.json`](task-definition.gateway.json)
  — gateway-api（HTTP 8000 + `/healthz`、ALB 配下）。secrets: DATABASE_URL/REDIS_URL/JWT_SECRET。
- [`task-definition.orchestrator-worker.json`](task-definition.orchestrator-worker.json)
  — 抽出ワーカー（キュー消費のため port/HTTP healthCheck なし）。secrets に ANTHROPIC_API_KEY。
- [`task-definition.export-worker.json`](task-definition.export-worker.json)
  — export ワーカー（q.export 消費 → canonical JSON/webhook 配信）。torch 非依存で軽量。
- [`task-definition.migrate.json`](task-definition.migrate.json)
  — Alembic マイグレーションの one-off タスク（`aws ecs run-task`）。
- [`task-definition.structure-svc.json`](task-definition.structure-svc.json)
  — PP-StructureV3（HTTP 8080 `/layout-parsing` + `/health`）。4 vCPU / 8GB。非公開（Service Connect）。
- [`task-definition.ocr-svc.json`](task-definition.ocr-svc.json)
  — PP-OCRv6 単体（HTTP 8080 `/ocr` + `/health`）。2 vCPU / 4GB。DD-02 の crop 再認識用。
- GPU 推論（Option B）は `requiresCompatibilities: ["EC2"]` ＋
  `resourceRequirements: [{"type":"GPU","value":"1"}]` を付け、GPU 容量プロバイダのクラスタへ配置する。

### 推論タスクのサイジング根拠（実測, linux/amd64 コンテナ）

sample2.png（A4 請求書 1 ページ, 740x1046）を `--engine onnxruntime` で warm 実測:

| vCPU | 1ページ処理 | 対 2vCPU |
|---|---|---|
| 2 | 111s | 1.0× |
| 4 | **51s** | 2.2× |
| 8 | 24s | 4.7× |
| 32 | 7.9s | 14× |

**vCPU にほぼ線形**（8→32 のみ 4倍CPUで3倍＝逓減）。ピークメモリは実測 **2.7GB**。

- **structure-svc = 4 vCPU / 8GB**: Fargate は 4 vCPU で最小 8GB のためメモリは十分（実使用 2.7GB）。
  線形スケールのため **1ページあたりのコストは 2/4/8 vCPU でほぼ同じ** → レイテンシ要件が無ければ
  小さめのタスクを**数で並べる**方がビンパッキング・耐障害性・キュー深度スケールと相性が良い。
  ページ latency を詰めたい場合は 8 vCPU / 16GB（24s/page、コストは 2 倍だがスループットも 2 倍）。
- **ocr-svc = 2 vCPU / 4GB**: crop した小画像の再認識が主でページ全体を処理しないため小さめ。
- スループットは**タスク数で線形に増やす**（キュー深度 → Application Auto Scaling、上表のとおり
  vCPU あたり性能がほぼ一定なので水平スケールが素直に効く）。

**コールドスタート注意**: モデル（数百MB〜）は初回起動時に `~/.paddlex` へ取得するため
`startPeriod: 300` を設定している。恒常運用ではモデルをイメージへ焼くか EFS でキャッシュを
共有して起動時間を短縮すること（付録C-4）。

## イメージ（ECR へ build/push）

Dockerfile は [`deploy/docker/`](../docker/)（ビルドコンテキスト = リポジトリルート）。

```bash
REG=<acct>.dkr.ecr.<region>.amazonaws.com
aws ecr get-login-password --region <region> | docker login --username AWS --password-stdin $REG
for svc in gateway orchestrator-worker export-worker migrate; do
  docker build -f deploy/docker/$svc.Dockerfile -t $REG/newfan-$svc:$IMAGE_TAG .
  docker push $REG/newfan-$svc:$IMAGE_TAG
done

# 推論は structure/ocr で同一イメージ（PIPELINE_CONFIG で使い分け）
docker build -f deploy/docker/inference.Dockerfile -t $REG/newfan-inference:$IMAGE_TAG .
docker push $REG/newfan-inference:$IMAGE_TAG
```

デプロイ順: ①`newfan-migrate` を run-task で単発実行 → ②gateway/worker サービス更新。

## ローカルとの対応

`deploy/compose.yaml`（1 service = 1 task definition）がローカルプロキシ。ローカルで
compose で通した構成をタスク定義に写像する。structure/ocr は Fargate 相当（CPU）で動くよう、
ローカルでも OpenVINO CPU イメージ／`--device cpu` で確認する。

## 未整備（TODO）

- IaC（Terraform 推奨）で cluster / service / ALB / WAF / autoscaling / Service Connect / ECR を定義。
- CI からの ECR build/push 自動化（現状は上記 build/push を手動）。
- RDS / ElastiCache（or SQS）/ EFS（memory-svc の FAISS スナップショット）のプロビジョニング。
- 済: サービス別 task-def（gateway/worker/migrate）と Dockerfile（[`deploy/docker/`](../docker/)）。
