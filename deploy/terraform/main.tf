locals {
  # 全リソース共通の接頭辞（例: ai-ocr-gateway）。別サービスと区別するため必ず ai-ocr を含む。
  prefix = var.name_prefix

  # ECR リポジトリ（CD の matrix.repo と一致させること）
  # inference は structure-svc / ocr-svc / structure-svc-seal が共有する単一イメージ
  # （中身は同じで pipeline_config だけ違う。compose も同じ構成）。
  images = ["gateway", "orchestrator-worker", "export-worker", "migrate", "inference"]

  # ロググループを作る task family
  log_families = ["gateway", "orchestrator-worker", "export-worker", "migrate", "structure-svc", "ocr-svc"]

  secret_arns = compact([
    aws_secretsmanager_secret.database_url.arn,
    aws_secretsmanager_secret.redis_url.arn,
    var.jwt_secret_arn,
    var.anthropic_secret_arn,
    var.gemini_secret_arn,
  ])
}

# --- ECR ---
resource "aws_ecr_repository" "this" {
  for_each             = toset(local.images)
  name                 = "${local.prefix}-${each.key}"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration {
    scan_on_push = true
  }
  tags = { Name = "${local.prefix}-${each.key}" }
}

# 推論イメージは 4.8GB（モデル焼き込み済み）。世代を残すとコストが効くため保持数を絞る。
resource "aws_ecr_lifecycle_policy" "this" {
  for_each   = aws_ecr_repository.this
  repository = each.value.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "直近 10 世代のみ保持"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 10 }
      action       = { type = "expire" }
    }]
  })
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
    actions   = ["s3:PutObject", "s3:GetObject"]
    resources = ["arn:aws:s3:::${var.export_s3_bucket}/*"]
  }
}

resource "aws_iam_role_policy" "task_s3" {
  name   = "export-s3"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task_s3.json
}

# --- ECS クラスタ（Fargate） ---
resource "aws_ecs_cluster" "app" {
  name = "${local.prefix}-${var.env}"
  tags = { Name = "${local.prefix}-${var.env}" }
  setting {
    name  = "containerInsights"
    value = "enabled"
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
  description = "AI-OCR の内部サービス名前解決（Service Connect）"
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
