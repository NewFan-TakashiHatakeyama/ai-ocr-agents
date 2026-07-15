# 推論サービング（PP-StructureV3 / PP-OCRv6, ADR-0003 Option A: Fargate CPU）。
#
# structure-svc と ocr-svc は同一イメージ（ai-ocr-inference）を共有し、entrypoint に渡す
# CONFIG/エンジンだけが違う（compose と同じ構成）。モデルはイメージに焼き込み済みのため
# 起動時のダウンロードは無い。
#
# 実測（sample2.png, warm, paddlepaddle 3.2.2）:
#   4 vCPU = 16.4s/枚、8 vCPU = 11.6s/枚、印章あり 8 vCPU = 11.5s/枚（+2%）
# paddlepaddle 3.3.1 は oneDNN/PIR のバグで 500 になるため Dockerfile で 3.2.2 に固定。

locals {
  # Service Connect の client_alias。orchestrator-worker はこの名前で解決する。
  structure_dns = "structure-svc"
  ocr_dns       = "ocr-svc"
  structure_url = "http://${local.structure_dns}:8080"
  ocr_url       = "http://${local.ocr_dns}:8080"

  # 印章オプション。印章版は別 config を読ませる（DD-03: モデル/機能は config が正、
  # クライアントはフラグを送らない）。
  structure_config = var.structure_seal_enabled ? "/opt/inference/structure/pipeline_config.seal.yaml" : "/opt/inference/structure/pipeline_config.yaml"
}

# ---------- structure-svc（PP-StructureV3 レイアウト+表+OCR） ----------
resource "aws_ecs_task_definition" "structure_svc" {
  family                   = "${local.prefix}-structure-svc"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = tostring(var.structure_cpu)
  memory                   = tostring(var.structure_memory)
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([{
    name         = "structure-svc"
    image        = "${aws_ecr_repository.this["inference"].repository_url}:${var.image_tag}"
    essential    = true
    portMappings = [{ name = "structure", containerPort = 8080, protocol = "tcp", appProtocol = "http" }]
    environment = [
      { name = "INFERENCE_ENGINE", value = "paddle" },
      { name = "PIPELINE_CONFIG", value = local.structure_config },
      { name = "TENANT_LANG", value = "ja" },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options   = merge(local.awslogs, { "awslogs-group" = aws_cloudwatch_log_group.this["structure-svc"].name })
    }
    healthCheck = {
      command  = ["CMD-SHELL", "python -c \"import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/health').status==200 else 1)\""]
      interval = 30
      timeout  = 5
      retries  = 3
      # モデルをイメージに焼いてもロードに時間がかかる（実測で 1 分弱）
      startPeriod = 120
    }
  }])

  tags = { Name = "${local.prefix}-structure-svc" }
}

resource "aws_ecs_service" "structure_svc" {
  name            = "${local.prefix}-structure-svc"
  cluster         = aws_ecs_cluster.app.id
  task_definition = aws_ecs_task_definition.structure_svc.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets         = local.private_subnet_ids
    security_groups = [aws_security_group.service.id]
  }

  service_connect_configuration {
    enabled   = true
    namespace = aws_service_discovery_http_namespace.this.arn
    service {
      port_name = "structure"
      client_alias {
        dns_name = local.structure_dns
        port     = 8080
      }
    }
  }

  tags = { Name = "${local.prefix}-structure-svc" }
}

# ---------- ocr-svc（DD-02 char_backfill 用の /ocr 単体） ----------
resource "aws_ecs_task_definition" "ocr_svc" {
  family                   = "${local.prefix}-ocr-svc"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = tostring(var.ocr_cpu)
  memory                   = tostring(var.ocr_memory)
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([{
    name         = "ocr-svc"
    image        = "${aws_ecr_repository.this["inference"].repository_url}:${var.image_tag}"
    essential    = true
    portMappings = [{ name = "ocr", containerPort = 8080, protocol = "tcp", appProtocol = "http" }]
    environment = [
      { name = "INFERENCE_ENGINE", value = "paddle" },
      { name = "PIPELINE_CONFIG", value = "/opt/inference/ocr/pipeline_config.yaml" },
      { name = "TENANT_LANG", value = "ja" },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options   = merge(local.awslogs, { "awslogs-group" = aws_cloudwatch_log_group.this["ocr-svc"].name })
    }
    healthCheck = {
      command     = ["CMD-SHELL", "python -c \"import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/health').status==200 else 1)\""]
      interval    = 30
      timeout     = 5
      retries     = 3
      startPeriod = 120
    }
  }])

  tags = { Name = "${local.prefix}-ocr-svc" }
}

resource "aws_ecs_service" "ocr_svc" {
  name            = "${local.prefix}-ocr-svc"
  cluster         = aws_ecs_cluster.app.id
  task_definition = aws_ecs_task_definition.ocr_svc.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets         = local.private_subnet_ids
    security_groups = [aws_security_group.service.id]
  }

  service_connect_configuration {
    enabled   = true
    namespace = aws_service_discovery_http_namespace.this.arn
    service {
      port_name = "ocr"
      client_alias {
        dns_name = local.ocr_dns
        port     = 8080
      }
    }
  }

  tags = { Name = "${local.prefix}-ocr-svc" }
}
