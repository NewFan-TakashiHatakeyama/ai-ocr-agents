# VL フォールバック（PaddleOCR-VL, §5.4 / DD-09）。GPU が要るのはここだけ。
#
# 構成:
#   vl-svc     : パイプライン（レイアウト検出＋整形）。**CPU / Fargate**。
#                VLRecognition は genai へ委譲するため GPU 不要（実測で確認）。
#   vlm-server : paddleocr genai_server（vLLM）。**GPU 必須**なので ECS on EC2。
#
# コスト（実価格）: g4dn.xlarge OnDemand $0.710/h。常時起動は $528/月でコストオーバー。
# ADR-0003 どおり VL は難読ページのみ（全体の約10%）で、月1万枚なら実処理は数時間。
# **既定は vl_enabled=false（GPU インスタンス 0 台＝課金なし）**。使う時だけ上げる:
#   scripts/aws_env.sh vl-up   /  vl-down
#
# GPU 課金は ASG の desired_capacity で切る。ECS サービスだけ 0 にしても EC2 は残るため。

locals {
  vl_dns = "vl-svc"
  vl_url = "http://${local.vl_dns}:8080"
  # vlm-server は同一タスク内ではなく GPU インスタンス上の別サービス。
  # vl-svc からは Service Connect の alias で引く。
  vlm_dns = "vlm-server"
  vlm_url = "http://${local.vlm_dns}:8080/v1"

  vl_desired  = var.vl_enabled ? 1 : 0
  vlm_desired = var.vl_enabled ? 1 : 0
}

# ---------- GPU キャパシティ（ECS on EC2） ----------

data "aws_ssm_parameter" "ecs_gpu_ami" {
  # ECS 最適化 GPU AMI（NVIDIA ドライバ + nvidia-container-toolkit 同梱）。
  name = "/aws/service/ecs/optimized-ami/amazon-linux-2023/gpu/recommended/image_id"
}

resource "aws_iam_role" "vlm_instance" {
  name = "${local.prefix}-vlm-instance"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
    }]
  })
  tags = { Name = "${local.prefix}-vlm-instance" }
}

resource "aws_iam_role_policy_attachment" "vlm_instance_ecs" {
  role       = aws_iam_role.vlm_instance.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEC2ContainerServiceforEC2Role"
}

resource "aws_iam_instance_profile" "vlm" {
  name = "${local.prefix}-vlm-instance"
  role = aws_iam_role.vlm_instance.name
}

resource "aws_launch_template" "vlm" {
  name_prefix   = "${local.prefix}-vlm-"
  image_id      = data.aws_ssm_parameter.ecs_gpu_ami.value
  instance_type = var.vl_instance_type

  iam_instance_profile { arn = aws_iam_instance_profile.vlm.arn }
  vpc_security_group_ids = [aws_security_group.service.id]

  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      # vlm-server イメージは 18.3GB（vLLM + CUDA）。既定 30GB では足りない。
      volume_size = var.vl_disk_gb
      volume_type = "gp3"
      encrypted   = true
    }
  }

  user_data = base64encode(<<-EOT
    #!/bin/bash
    echo "ECS_CLUSTER=${aws_ecs_cluster.app.name}" >> /etc/ecs/ecs.config
    echo "ECS_ENABLE_GPU_SUPPORT=true" >> /etc/ecs/ecs.config
  EOT
  )

  tag_specifications {
    resource_type = "instance"
    tags          = { Name = "${local.prefix}-vlm" }
  }
  tags = { Name = "${local.prefix}-vlm" }
}

resource "aws_autoscaling_group" "vlm" {
  name                = "${local.prefix}-vlm"
  vpc_zone_identifier = local.private_subnet_ids
  # vl_enabled=false で 0 台 → GPU 課金なし。これが VL の on/off スイッチ。
  min_size         = 0
  max_size         = var.vl_enabled ? 1 : 0
  desired_capacity = local.vlm_desired

  launch_template {
    id      = aws_launch_template.vlm.id
    version = "$Latest"
  }

  # ECS のキャパシティプロバイダが管理する
  protect_from_scale_in = false

  tag {
    key                 = "Name"
    value               = "${local.prefix}-vlm"
    propagate_at_launch = true
  }
  tag {
    key                 = "Service"
    value               = "ai-ocr"
    propagate_at_launch = true
  }
  tag {
    key                 = "AmazonECSManaged"
    value               = "true"
    propagate_at_launch = true
  }
}

resource "aws_ecs_capacity_provider" "vlm" {
  name = "${local.prefix}-vlm"
  auto_scaling_group_provider {
    auto_scaling_group_arn = aws_autoscaling_group.vlm.arn
    # スケールは terraform（vl_enabled）で決める。ECS に勝手に増やされないようにする。
    managed_scaling {
      status = "DISABLED"
    }
    managed_termination_protection = "DISABLED"
  }
  tags = { Name = "${local.prefix}-vlm" }
}

resource "aws_ecs_cluster_capacity_providers" "vlm" {
  cluster_name = aws_ecs_cluster.app.name
  capacity_providers = concat(
    ["FARGATE", "FARGATE_SPOT"],
    var.vl_enabled ? [aws_ecs_capacity_provider.vlm.name] : [],
  )
  default_capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
  }
}

# ---------- vlm-server（GPU / EC2） ----------

resource "aws_ecs_task_definition" "vlm_server" {
  family                   = "${local.prefix}-vlm-server"
  requires_compatibilities = ["EC2"] # GPU は Fargate 非対応
  network_mode             = "awsvpc"
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([{
    name                 = "vlm-server"
    image                = "${aws_ecr_repository.this["vlm-server"].repository_url}:${var.image_tag}"
    essential            = true
    cpu                  = 3072
    memory               = 12288
    portMappings         = [{ name = "vlm", containerPort = 8080, protocol = "tcp", appProtocol = "http" }]
    resourceRequirements = [{ type = "GPU", value = "1" }]
    logConfiguration = {
      logDriver = "awslogs"
      options   = merge(local.awslogs, { "awslogs-group" = aws_cloudwatch_log_group.this["vlm-server"].name })
    }
    healthCheck = {
      command  = ["CMD-SHELL", "curl -fsS http://localhost:8080/health || exit 1"]
      interval = 30
      timeout  = 10
      retries  = 10
      # ECS の startPeriod は最大 300 秒（それ以上は RegisterTaskDefinition が 400。実測）。
      # vLLM のロードが 300 秒を超える場合は retries で吸収する（30s x 10 = 5 分の猶予）。
      startPeriod = 300
    }
  }])

  tags = { Name = "${local.prefix}-vlm-server" }
}

resource "aws_ecs_service" "vlm_server" {
  count           = var.vl_enabled ? 1 : 0
  name            = "${local.prefix}-vlm-server"
  cluster         = aws_ecs_cluster.app.id
  task_definition = aws_ecs_task_definition.vlm_server.arn
  desired_count   = local.vlm_desired

  capacity_provider_strategy {
    capacity_provider = aws_ecs_capacity_provider.vlm.name
    weight            = 1
  }

  network_configuration {
    subnets         = local.private_subnet_ids
    security_groups = [aws_security_group.service.id]
  }

  service_connect_configuration {
    enabled   = true
    namespace = aws_service_discovery_http_namespace.this.arn
    service {
      port_name = "vlm"
      client_alias {
        dns_name = local.vlm_dns
        port     = 8080
      }
      timeout {
        per_request_timeout_seconds = 300
        idle_timeout_seconds        = 600
      }
    }
  }

  depends_on = [aws_ecs_cluster_capacity_providers.vlm]
  tags       = { Name = "${local.prefix}-vlm-server" }
}

# ---------- vl-svc（パイプライン / CPU Fargate） ----------

resource "aws_ecs_task_definition" "vl_svc" {
  family                   = "${local.prefix}-vl-svc"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = tostring(var.vl_cpu)
  memory                   = tostring(var.vl_memory)
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([{
    name         = "vl-svc"
    image        = "${aws_ecr_repository.this["vl-pipeline"].repository_url}:${var.image_tag}"
    essential    = true
    portMappings = [{ name = "vl", containerPort = 8080, protocol = "tcp", appProtocol = "http" }]
    environment = [
      { name = "PIPELINE_CONFIG", value = "/opt/inference/vl/pipeline_config.yaml" },
      { name = "INFERENCE_ENGINE", value = "paddle" },
      { name = "TENANT_LANG", value = "ja" },
      # config の server_url を実際の Service Connect 名に差し替える（entrypoint が反映）
      { name = "VLM_SERVER_URL", value = local.vlm_url },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options   = merge(local.awslogs, { "awslogs-group" = aws_cloudwatch_log_group.this["vl-svc"].name })
    }
    healthCheck = {
      command     = ["CMD-SHELL", "python -c \"import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/health').status==200 else 1)\""]
      interval    = 30
      timeout     = 5
      retries     = 3
      startPeriod = 180
    }
  }])

  tags = { Name = "${local.prefix}-vl-svc" }
}

resource "aws_ecs_service" "vl_svc" {
  count           = var.vl_enabled ? 1 : 0
  name            = "${local.prefix}-vl-svc"
  cluster         = aws_ecs_cluster.app.id
  task_definition = aws_ecs_task_definition.vl_svc.arn
  desired_count   = local.vl_desired
  launch_type     = "FARGATE"

  network_configuration {
    subnets         = local.private_subnet_ids
    security_groups = [aws_security_group.service.id]
  }

  service_connect_configuration {
    enabled   = true
    namespace = aws_service_discovery_http_namespace.this.arn
    service {
      port_name = "vl"
      client_alias {
        dns_name = local.vl_dns
        port     = 8080
      }
      timeout {
        per_request_timeout_seconds = 300
        idle_timeout_seconds        = 600
      }
    }
  }

  tags = { Name = "${local.prefix}-vl-svc" }
}
