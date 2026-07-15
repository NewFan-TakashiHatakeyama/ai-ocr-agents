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
- [`task-definition.structure-svc-seal.json`](task-definition.structure-svc-seal.json)
  — **印章ありオプション**。同一イメージで `PIPELINE_CONFIG` を seal 版に差し替えるだけ。
  印章の文字（角印の社名等）が要件にある場合に使う。速度・コストは既定とほぼ同じ。
- [`task-definition.ocr-svc.json`](task-definition.ocr-svc.json)
  — PP-OCRv6 単体（HTTP 8080 `/ocr` + `/health`）。2 vCPU / 4GB。DD-02 の crop 再認識用。
- GPU 推論（Option B）は `requiresCompatibilities: ["EC2"]` ＋
  `resourceRequirements: [{"type":"GPU","value":"1"}]` を付け、GPU 容量プロバイダのクラスタへ配置する。

### エンジンは paddle（＋oneDNN）。paddlepaddle は **3.2.2 固定**が必須

**最新の paddle 3.3.1 を使ってはいけない**。3.3.1 は oneDNN/PIR の未実装パスに当たり
`paddlex --serve` の `/layout-parsing` が必ず 500 になる（`ConvertPirAttribute2RuntimeAttribute`,
`onednn_instruction.cc:116`）。回避手段が無く（CLI にフラグ無し・`FLAGS_use_mkldnn=0` も無効と実測）、
3.3.1 では onnxruntime しか使えず印章オプションも起動できない。
**3.2.2 では oneDNN が正常動作**し、同一精度のまま onnxruntime より約2〜3倍速い。
paddlex 3.7.2 の HPI 対応表も paddle30/31/311 までで 3.3 系は「未対応」扱い。

### 推論タスクのサイジング根拠（実測, linux/amd64 コンテナ, sample2.png A4請求書1ページ warm）

| 構成 | 4 vCPU | 8 vCPU | vCPU秒/page（コスト目安） |
|---|---|---|---|
| paddle **3.3.1** | ✗ 500 | ✗ 500 | — |
| onnxruntime | 51.2s | 23.7s | 190〜205 |
| **paddle 3.2.2 + oneDNN** | **16.4s** | **11.6s** | **65.6** / 92.8 |
| paddle 3.2.2 + 印章ON | — | 11.5s | — |

ピークメモリ実測 **2.7GB**。精度は全構成で同一（spans=94 / 13行 / conf 0.9678）。

- **structure-svc = 4 vCPU / 8GB**（16.4s/page）: **vCPU秒/page が 65.6 と最良**＝最もコスト効率が良い。
  Fargate は 4 vCPU で最小 8GB（実使用 2.7GB のため余裕）。latency を詰めるなら 8 vCPU/16GB
  （11.6s/page、コスト 1.4 倍）。
- **structure-svc-seal = 4 vCPU / 8GB**: 印章ありオプション。**印章のコストはほぼゼロ**
  （8vCPU 実測: 印章ON 11.5s vs OFF 11.6s）。engine=paddle 必須（onnx パッケージが無いため）。
- **ocr-svc = 2 vCPU / 4GB**: crop した小画像の再認識が主でページ全体を処理しないため小さめ。
- スループットは**タスク数で増やす**（キュー深度 → Application Auto Scaling）。

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
