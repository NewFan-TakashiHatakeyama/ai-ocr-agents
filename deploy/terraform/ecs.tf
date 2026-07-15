locals {
  awslogs = {
    "awslogs-region"        = var.aws_region
    "awslogs-stream-prefix" = "ecs"
  }
}

# ---------- gateway ----------
resource "aws_ecs_task_definition" "gateway" {
  family                   = "${local.prefix}-gateway"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([{
    name         = "gateway"
    image        = "${aws_ecr_repository.this["gateway"].repository_url}:${var.image_tag}"
    essential    = true
    portMappings = [{ name = "gateway", containerPort = 8000, protocol = "tcp", appProtocol = "http" }]
    environment = [
      { name = "APP_ENV", value = var.env },
      { name = "AWS_REGION", value = var.aws_region },
      { name = "CORS_ORIGINS", value = var.cors_origins },
      # 未設定だと ingest が LocalObjectStore になり file:///app/data/... を返す。
      # Fargate のディスクはタスク毎に独立なので、gateway(2台) 間でも
      # orchestrator-worker からも読めず抽出が失敗する。
      { name = "S3_BUCKET", value = aws_s3_bucket.this.id },
      { name = "S3_KMS_KEY_ID", value = var.s3_kms_key_id },
      # チャットグラフ（§4.5）の supervisor が LLM を呼ぶ。未設定だと
      # RuleBasedChatAgent に落ち、ツールが一切使われない（実 AWS で検出）。
      { name = "LLM_PROVIDER", value = var.llm_provider },
      { name = "LLM_MODEL", value = var.llm_model },
    ]
    secrets = concat([
      { name = "DATABASE_URL", valueFrom = aws_secretsmanager_secret.database_url.arn },
      { name = "REDIS_URL", valueFrom = aws_secretsmanager_secret.redis_url.arn },
      { name = "JWT_SECRET", valueFrom = local.jwt_secret_arn },
      ], var.anthropic_secret_arn == "" ? [] : [
      { name = "ANTHROPIC_API_KEY", valueFrom = var.anthropic_secret_arn },
      ], var.gemini_secret_arn == "" ? [] : [
      { name = "GEMINI_API_KEY", valueFrom = var.gemini_secret_arn },
    ])
    logConfiguration = {
      logDriver = "awslogs"
      options   = merge(local.awslogs, { "awslogs-group" = aws_cloudwatch_log_group.this["gateway"].name })
    }
    healthCheck = {
      command     = ["CMD-SHELL", "python -c \"import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz').status==200 else 1)\""]
      interval    = 30
      timeout     = 5
      retries     = 3
      startPeriod = 30
    }
  }])
}

resource "aws_ecs_service" "gateway" {
  name            = "${local.prefix}-gateway"
  cluster         = aws_ecs_cluster.app.id
  task_definition = aws_ecs_task_definition.gateway.arn
  desired_count   = local.desired.gateway
  launch_type     = "FARGATE"

  network_configuration {
    subnets         = local.private_subnet_ids
    security_groups = [aws_security_group.service.id]
  }
  load_balancer {
    target_group_arn = aws_lb_target_group.gateway.arn
    container_name   = "gateway"
    container_port   = 8000
  }
  depends_on = [aws_lb_listener.http]
}

# ---------- orchestrator-worker ----------
resource "aws_ecs_task_definition" "orchestrator_worker" {
  family                   = "${local.prefix}-orchestrator-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "1024"
  memory                   = "3072"
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([{
    name      = "orchestrator-worker"
    image     = "${aws_ecr_repository.this["orchestrator-worker"].repository_url}:${var.image_tag}"
    essential = true
    command   = ["python", "-m", "newfan_orchestrator.worker_main"]
    # VL_URL は vl_enabled のときだけ渡す。worker_main は未設定なら VL を配線せず、
    # 難読ページは HITL 直行に縮退する（§2.6 / ADR-0003）。
    environment = concat([
      { name = "APP_ENV", value = var.env },
      { name = "AWS_REGION", value = var.aws_region },
      { name = "LLM_MODEL", value = var.llm_model },
      { name = "STRUCTURE_URL", value = local.structure_url },
      { name = "OCR_URL", value = local.ocr_url },
      { name = "LLM_PROVIDER", value = var.llm_provider },
      ], var.vl_enabled ? [
      { name = "VL_URL", value = local.vl_url },
    ] : [])
    # LLM_PROVIDER=gemini のときは GEMINI_API_KEY が要る（未設定だと provider 生成で落ちる）
    secrets = concat([
      { name = "DATABASE_URL", valueFrom = aws_secretsmanager_secret.database_url.arn },
      { name = "REDIS_URL", valueFrom = aws_secretsmanager_secret.redis_url.arn },
      ], var.anthropic_secret_arn == "" ? [] : [
      { name = "ANTHROPIC_API_KEY", valueFrom = var.anthropic_secret_arn },
      ], var.gemini_secret_arn == "" ? [] : [
      { name = "GEMINI_API_KEY", valueFrom = var.gemini_secret_arn },
    ])
    logConfiguration = {
      logDriver = "awslogs"
      options   = merge(local.awslogs, { "awslogs-group" = aws_cloudwatch_log_group.this["orchestrator-worker"].name })
    }
  }])

  tags = { Name = "${local.prefix}-orchestrator-worker" }
}

resource "aws_ecs_service" "orchestrator_worker" {
  name            = "${local.prefix}-orchestrator-worker"
  cluster         = aws_ecs_cluster.app.id
  task_definition = aws_ecs_task_definition.orchestrator_worker.arn
  desired_count   = local.desired.orchestrator
  launch_type     = "FARGATE"
  network_configuration {
    subnets         = local.private_subnet_ids
    security_groups = [aws_security_group.service.id]
  }

  # クライアントとして参加する（service を公開しない）。これが無いと
  # STRUCTURE_URL=http://structure-svc:8080 が名前解決できない。
  service_connect_configuration {
    enabled   = true
    namespace = aws_service_discovery_http_namespace.this.arn
  }

  tags = { Name = "${local.prefix}-orchestrator-worker" }
}

# ---------- export-worker ----------
resource "aws_ecs_task_definition" "export_worker" {
  family                   = "${local.prefix}-export-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([{
    name      = "export-worker"
    image     = "${aws_ecr_repository.this["export-worker"].repository_url}:${var.image_tag}"
    essential = true
    command   = ["python", "-m", "newfan_export.worker_main"]
    environment = [
      { name = "APP_ENV", value = var.env },
      { name = "AWS_REGION", value = var.aws_region },
      { name = "EXPORT_S3_BUCKET", value = aws_s3_bucket.this.id },
      { name = "EXPORT_S3_KMS_KEY_ID", value = var.s3_kms_key_id },
    ]
    secrets = [
      { name = "DATABASE_URL", valueFrom = aws_secretsmanager_secret.database_url.arn },
      { name = "REDIS_URL", valueFrom = aws_secretsmanager_secret.redis_url.arn },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options   = merge(local.awslogs, { "awslogs-group" = aws_cloudwatch_log_group.this["export-worker"].name })
    }
  }])
}

resource "aws_ecs_service" "export_worker" {
  name            = "${local.prefix}-export-worker"
  cluster         = aws_ecs_cluster.app.id
  task_definition = aws_ecs_task_definition.export_worker.arn
  desired_count   = local.desired.export
  launch_type     = "FARGATE"
  network_configuration {
    subnets         = local.private_subnet_ids
    security_groups = [aws_security_group.service.id]
  }
}

# ---------- migrate（サービス化せず run-task で単発実行） ----------
resource "aws_ecs_task_definition" "migrate" {
  family                   = "${local.prefix}-migrate"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "256"
  memory                   = "512"
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([{
    name      = "migrate"
    image     = "${aws_ecr_repository.this["migrate"].repository_url}:${var.image_tag}"
    essential = true
    command   = ["alembic", "-c", "db/alembic.ini", "upgrade", "head"]
    secrets   = [{ name = "DATABASE_URL", valueFrom = aws_secretsmanager_secret.database_url.arn }]
    logConfiguration = {
      logDriver = "awslogs"
      options   = merge(local.awslogs, { "awslogs-group" = aws_cloudwatch_log_group.this["migrate"].name })
    }
  }])
}

# ---------- web（Next.js / SCR-01〜。ALB の既定ターゲット） ----------
resource "aws_ecs_task_definition" "web" {
  family                   = "${local.prefix}-web"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([{
    name         = "web"
    image        = "${aws_ecr_repository.this["web"].repository_url}:${var.image_tag}"
    essential    = true
    portMappings = [{ name = "web", containerPort = 3000, protocol = "tcp", appProtocol = "http" }]
    # NEXT_PUBLIC_* はビルド時にバンドルへ焼き込まれるため実行時 env では変えられない
    # （API のベース URL は docker build --build-arg NEXT_PUBLIC_API_BASE で渡す）。
    environment = [
      { name = "NODE_ENV", value = "production" },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options   = merge(local.awslogs, { "awslogs-group" = aws_cloudwatch_log_group.this["web"].name })
    }
  }])

  tags = { Name = "${local.prefix}-web" }
}

resource "aws_ecs_service" "web" {
  name            = "${local.prefix}-web"
  cluster         = aws_ecs_cluster.app.id
  task_definition = aws_ecs_task_definition.web.arn
  desired_count   = local.desired.web
  launch_type     = "FARGATE"

  network_configuration {
    subnets         = local.private_subnet_ids
    security_groups = [aws_security_group.service.id]
  }
  load_balancer {
    target_group_arn = aws_lb_target_group.web.arn
    container_name   = "web"
    container_port   = 3000
  }
  depends_on = [aws_lb_listener.http]

  tags = { Name = "${local.prefix}-web" }
}
