locals {
  # ECR リポジトリ（CD の matrix.repo と一致）
  images = ["gateway", "orchestrator-worker", "export-worker", "migrate"]
  # ロググループを作る task family
  log_families = ["gateway", "orchestrator-worker", "export-worker", "migrate"]
  secret_arns = [
    var.db_secret_arn,
    var.redis_secret_arn,
    var.jwt_secret_arn,
    var.anthropic_secret_arn,
  ]
}

# --- ECR ---
resource "aws_ecr_repository" "this" {
  for_each             = toset(local.images)
  name                 = "newfan-${each.key}"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration {
    scan_on_push = true
  }
}

# --- CloudWatch Logs ---
resource "aws_cloudwatch_log_group" "this" {
  for_each          = toset(local.log_families)
  name              = "/ecs/newfan-${each.key}"
  retention_in_days = 30
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
  name               = "newfan-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
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
  name               = "newfan-ecs-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
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
  name = "newfan-app"
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_ecs_cluster_capacity_providers" "app" {
  cluster_name       = aws_ecs_cluster.app.name
  capacity_providers = ["FARGATE", "FARGATE_SPOT"]
  default_capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
  }
}
