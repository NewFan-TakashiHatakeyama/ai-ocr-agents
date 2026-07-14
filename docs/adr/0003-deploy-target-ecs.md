# ADR-0003: 本番プラットフォームを ECS（Fargate 中心）とする（EKS からの変更）

- 状態: Accepted
- 日付: 2026-07-14
- 関連: 詳細設計 §2.3（K8s/EKS想定）/ §2.6（オンプレCPU）/ §13.3（HPA）、DD-05

## コンテキスト

詳細設計 §2.3 は本番を Kubernetes（EKS）想定としていた。しかし EKS は
運用・実装コストが大きい（クラスタ運用、ノードプール管理、コントロールプレーン費用）。
本プロジェクトはコストと立ち上げ速度を優先する。

## 決定

本番プラットフォームを **Amazon ECS** に変更する。CPU 系は **Fargate**（サーバレス、
ノード管理不要）を基本とし、GPU が必須のコンポーネントのみ **ECS on EC2（GPU）** を使う。

ECS で最大限進めるための鍵は「GPU を避けられる範囲を最大化する」こと。Fargate は
**GPU 非対応**のため、GPU が要るのは自己ホスト推論（PaddleOCR サービング）だけである。

### トポロジ選択肢

| | Option A（推奨・最小コスト/運用） | Option B（スループット重視） |
|---|---|---|
| app services | Fargate | Fargate |
| structure-svc / ocr-svc | **Fargate（OpenVINO CPU, §2.6）** | ECS on EC2 GPU（g5） |
| vl-svc（フォールバック専用, 全体の約10%） | 当面 **無効化**（§2.6 の VL 無し縮退＝HITL直行）／後日 GPU 追加 | ECS on EC2 GPU（g5, 専用） |
| GPU EC2 の要否 | 不要（VL 有効化時のみ） | 必要 |

**コスト優先の本プロジェクトでは Option A を既定**とする。主経路（structure/ocr）を
OpenVINO CPU で Fargate に載せれば GPU インスタンスを完全に回避でき、運用も Fargate だけで
完結する。VL は当面無効（難読ページは HITL 直行）とし、精度要件が満たせない場合に限り
Option B（vl-svc を ECS-EC2-GPU で追加）へ段階拡張する。コード分岐は設定で吸収する（§2.6 方針）。

## コンポーネント → ECS マッピング

| 設計 §2.1 | ECS 構成 | 備考 |
|---|---|---|
| gateway-api | Fargate service ＋ **ALB + WAF**（外部公開） | 設計の外部公開方針を踏襲 |
| web (Next.js) | Fargate service ＋ ALB + WAF | |
| orchestrator-svc | Fargate service | キュー深度でオートスケール（下記） |
| ingest-svc | Fargate service | pypdfium2/LibreOffice は CPU、Fargate で可 |
| memory-svc | Fargate service ＋ **EFS**（FAISS スナップショット） | 正本は RDS、index は再構築可（DD-07） |
| export-svc / llm-adapter | Fargate service | |
| structure-svc / ocr-svc | Fargate（OpenVINO CPU）／ Option B は EC2-GPU | 推論イメージは ECR |
| vl-svc | 既定無効／ Option B は EC2-GPU（g5, 専用容量） | GPU 分離（§2.3 の gpu-vl プールに相当） |
| PostgreSQL | **RDS**（マネージド） | 変更なし |
| Redis | **ElastiCache**、またはキューは **SQS**（DD-05） | AWS 本番は SQS が自然 |
| S3 | マネージド（SSE-KMS） | 変更なし |
| Neo4j | ECS-EC2 常駐 or マネージド or 当面保留 | ルール関係グラフ。MVP 後段 |
| Langfuse | Fargate service or マネージド | |

## K8s 機能の ECS への置換

- **HPA（CPU）→ ECS Service Auto Scaling**（ターゲット追跡: CPU / `ALBRequestCountPerTarget`）。
- **HPA（キュー深度: orchestrator/structure）→ Application Auto Scaling ＋ CloudWatch カスタム
  メトリクス**（SQS なら `ApproximateNumberOfMessagesVisible`、Redis Streams なら独自メトリクス）。
  設計 §13.3「深度基準」をそのまま踏襲。
- **ClusterIP 内部公開 → ECS Service Connect（Cloud Map）**。推論・内部サービスは公開しない。
- **ALB + WAF（外部）→ 変更なし**（gateway/web のみ）。
- **ノードプール分離（general/gpu-ocr/gpu-vl）→ ECS クラスタ/容量プロバイダの分離**
  （Fargate クラスタ ＋ 必要時に GPU EC2 容量プロバイダ）。
- **Secrets → AWS Secrets Manager**（§16.5 の secret_ref と整合）。**設定 → SSM Parameter Store /
  環境変数**（12-factor, §2.4）。イメージは **ECR**。

## 変わらないもの

- アプリコードは 12-factor でプラットフォーム非依存（K8s 結合なし）。**ECS 化でアプリ実装の変更は不要**。
- ローカル/CI は `deploy/compose.yaml`（コンテナベース）を継続。compose は ECS タスク定義の
  ローカルプロキシとして機能する（1 service = 1 task definition に素直に対応）。
- オンプレ CPU 軽量版（§2.6）は本 ADR の Option A とほぼ同一思想（VL 無し・OpenVINO CPU）。

## 影響・TODO

- 反映済み: 設計書 v1.2（`NewFan_AI-OCRエージェント詳細設計書_v1.2.md`）で §2.3／§13.3 を ECS 前提に更新、DD-14／DD-15 を追加。
- `deploy/ecs/` に ECS タスク定義テンプレートを追加済み。**次段: IaC（Terraform 推奨）で cluster/service/task-def/ALB/WAF/autoscaling/Service Connect を定義（Option A 前提）**。
- Option A で VL を無効化する場合、品質ゲート NG ページは HITL 直行（§10 の VL 縮退と同じ経路）。
