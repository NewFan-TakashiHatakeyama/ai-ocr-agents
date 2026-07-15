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

### エンジンは onnxruntime 一択（性能選択ではなく制約）

`--engine paddle`(=paddle_static) は **paddle 3.3.1 の oneDNN/PIR 未実装**により
`/layout-parsing` が必ず 500 になる（`ConvertPirAttribute2RuntimeAttribute`,
`onednn_instruction.cc:116`）。Python API は `enable_mkldnn=False` で回避できるが、
`paddlex --serve` に無効化手段が無い（CLI にフラグ無し・`FLAGS_use_mkldnn=0` も無効と実測）。
`paddle_dynamic` は一部モデルが非対応。→ **サービングで動くのは onnxruntime のみ**。

### 推論タスクのサイジング根拠（実測, linux/amd64 コンテナ, onnxruntime）

sample2.png（A4 請求書 1 ページ, 740x1046）warm 実測:

| vCPU | 1ページ | vCPU秒/page（コスト目安） |
|---|---|---|
| 2 | 111s | 222 |
| 4 | 51s | 205 |
| **8** | **24s** | **190** |
| 16 | **11.3s** | 181 |
| 32 | 7.9s | 253 |

onnxruntime は **16 vCPU まで良くスケール**（8→16 で 2.1×＝ほぼ線形）。ピークメモリ実測 **2.7GB**。
1ページ単価は 4〜16 vCPU でほぼ横ばい（181〜205 vCPU秒）＝**大きいタスクにしても割高にならない**。

- **structure-svc = 8 vCPU / 16GB**（24s/page）: 単価が最良付近で latency も実用的。Fargate は
  8 vCPU で最小 16GB（実使用 2.7GB のため余裕）。**さらに latency を詰めるなら 16 vCPU/32GB
  （11.3s/page、単価はむしろ最安）**。逆に細かく刻みたいなら 4 vCPU/8GB（51s/page）。
- **ocr-svc = 2 vCPU / 4GB**: crop した小画像の再認識が主でページ全体を処理しないため小さめ。
- スループットは**タスク数で増やす**（キュー深度 → Application Auto Scaling）。単価がほぼ一定なので
  水平・垂直どちらでもコストは同等 → 運用しやすい水平スケールを基本にする。

**コールドスタート**: モデルはイメージへ焼き込み済み（`prefetch_models.py`）。起動時に
外部（百度 CDN `bcebos.com`）へ数百MB取りに行かないため、起動が決定論的で外部依存も無い。
実測: 焼き込み **19秒** / 未焼き込み 26秒（ローカル回線）。`startPeriod: 120` で足りる。

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
