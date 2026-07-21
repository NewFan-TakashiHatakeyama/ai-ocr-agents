locals {
  # services_enabled=false で全サービスを 0 台に落とす（起動/停止スイッチ）。
  # ここを 0 にしても RDS/Redis/NAT/ALB は残る（AWS 側に停止が無いため）。完全に止めるなら destroy。
  desired = {
    gateway      = var.services_enabled ? 2 : 0
    orchestrator = var.services_enabled ? 1 : 0
    export       = var.services_enabled ? 1 : 0
    structure    = var.services_enabled ? 1 : 0
    ocr          = var.services_enabled ? 1 : 0
    web          = var.services_enabled ? 1 : 0
  }

  # 全リソース共通の接頭辞（例: ai-ocr-gateway）。別サービスと区別するため必ず ai-ocr を含む。
  prefix = var.name_prefix

  # ECR リポジトリ（CD の matrix.repo と一致させること）
  # inference は structure-svc / ocr-svc / structure-svc-seal が共有する単一イメージ
  # （中身は同じで pipeline_config だけ違う。compose も同じ構成）。
  images = ["gateway", "orchestrator-worker", "export-worker", "migrate", "inference", "web", "vl-pipeline", "vlm-server"]

  # ロググループを作る task family
  log_families = ["gateway", "orchestrator-worker", "export-worker", "migrate", "structure-svc", "ocr-svc", "web", "vl-svc", "vlm-server"]

  # 既存 ARN が渡されなければ本スタックが作った JWT 鍵を使う
  jwt_secret_arn = var.jwt_secret_arn != "" ? var.jwt_secret_arn : aws_secretsmanager_secret.jwt[0].arn

  # §7.3 RLS: アプリは所有者でないロールで繋ぐ。所有者（マスタ newfan）は RLS を
  # バイパスするため、アプリがそれを使うとテナント分離が全く効かない（実 RDS で計測済み）。
  # 所有者は migrate/バッチ専用に残す。
  db_app_username = "newfan_app"

  secret_arns = compact([
    aws_secretsmanager_secret.database_url.arn,
    aws_secretsmanager_secret.app_database_url.arn,
    aws_secretsmanager_secret.redis_url.arn,
    local.jwt_secret_arn,
    var.anthropic_secret_arn,
    var.gemini_secret_arn,
  ])
}

# --- ECR ---
# ECR は本スタックでは作らない。deploy/terraform/ecr（長命な別スタック）が持つ。
# down（destroy）でイメージ 30GB を消すと次回の up が再ビルド＋再 push で 30 分以上に
# なるため。同一 state に置くと destroy が RepositoryNotEmptyException で失敗し、
# down 全体が完遂しなかった（実際に踏んだ）。
# scripts/aws_env.sh up が ecr スタックを先に apply する（冪等）。
data "aws_ecr_repository" "this" {
  for_each = toset(local.images)
  name     = "${local.prefix}-${each.key}"
}

# --- CloudWatch Logs ---
resource "aws_cloudwatch_log_group" "this" {
  for_each          = toset(local.log_families)
  name              = "/ecs/${local.prefix}-${each.key}"
  retention_in_days = 30
  tags              = { Name = "/ecs/${local.prefix}-${each.key}" }
}

# --- IAM: タスク実行ロール（イメージ pull / logs / secrets 取得） ---
data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "${local.prefix}-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
  tags               = { Name = "${local.prefix}-ecs-execution" }
}

resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "secrets_read" {
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = local.secret_arns
  }
}

resource "aws_iam_role_policy" "execution_secrets" {
  name   = "read-secrets"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.secrets_read.json
}

# --- IAM: タスクロール（アプリ権限: export S3） ---
resource "aws_iam_role" "task" {
  name               = "${local.prefix}-ecs-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
  tags               = { Name = "${local.prefix}-ecs-task" }
}

data "aws_iam_policy_document" "task_s3" {
  statement {
    actions   = ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.this.arn}/*"]
  }
  statement {
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.this.arn]
  }
  # S3ObjectStore.put は KMS キー未指定でも SSE-KMS で PUT する。この権限が無いと
  # アップロードが AccessDenied で落ちる（取得側も Decrypt が要る）。
  # 対象を S3 経由の利用に限定する。
  statement {
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["s3.${var.aws_region}.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "task_s3" {
  name   = "export-s3"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task_s3.json
}

# 接続の秘密（§16.5 / P6）。connections は secret_ref（ARN）だけを DB に持ち、
# 実体は ai-ocr/<env>/conn/ 配下の Secrets Manager にある。gateway が webhook 署名鍵を
# 作成し、orchestrator が sink.db_write / webhook 配信時に実行時解決する。
# 名前プレフィクスでスコープし、LLM キーや DB URL 等の他 secret には触れない。
data "aws_iam_policy_document" "task_conn_secrets" {
  statement {
    actions = [
      "secretsmanager:GetSecretValue",
      "secretsmanager:DescribeSecret",
      "secretsmanager:CreateSecret",
      "secretsmanager:PutSecretValue",
    ]
    resources = ["arn:aws:secretsmanager:${var.aws_region}:*:secret:ai-ocr/${var.env}/conn/*"]
  }
}

resource "aws_iam_role_policy" "task_conn_secrets" {
  name   = "conn-secrets"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task_conn_secrets.json
}

# --- ECS クラスタ（Fargate） ---
resource "aws_ecs_cluster" "app" {
  name = "${local.prefix}-${var.env}"
  tags = { Name = "${local.prefix}-${var.env}" }
  # Container Insights は約 $18/月（メトリクス課金）。開発環境では切れるようにする。
  setting {
    name  = "containerInsights"
    value = var.container_insights_enabled ? "enabled" : "disabled"
  }

  # STRUCTURE_URL=http://structure-svc:8080 を名前解決するための Service Connect 名前空間。
  # これが無いと orchestrator-worker は推論サービングに到達できない。
  service_connect_defaults {
    namespace = aws_service_discovery_http_namespace.this.arn
  }
}

# Service Connect 用の名前空間（AI-OCR 専用。別サービスと混ざらないよう prefix を付ける）
resource "aws_service_discovery_http_namespace" "this" {
  name        = "${local.prefix}-${var.env}"
  description = "AI-OCR internal service discovery (Service Connect)"
  tags        = { Name = "${local.prefix}-${var.env}" }
}

resource "aws_ecs_cluster_capacity_providers" "app" {
  cluster_name       = aws_ecs_cluster.app.name
  capacity_providers = ["FARGATE", "FARGATE_SPOT"]
  default_capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
  }
}
