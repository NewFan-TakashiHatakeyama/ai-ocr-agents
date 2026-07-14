# ECS クラスタ（app / inference）と Service Connect 名前空間（§2.3 / DD-14）

resource "aws_ecs_cluster" "app" {
  name = "${var.project}-app"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_ecs_cluster" "inference" {
  name = "${var.project}-inference"

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
    base              = 1
    weight            = 1
  }
}

resource "aws_ecs_cluster_capacity_providers" "inference" {
  cluster_name       = aws_ecs_cluster.inference.name
  capacity_providers = ["FARGATE"]

  default_capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
  }
}

# Service Connect（Cloud Map）: 内部 DNS 名前空間 newfan.internal
resource "aws_service_discovery_http_namespace" "internal" {
  name        = "${var.project}.internal"
  description = "ECS Service Connect namespace for internal service-to-service calls"
}

locals {
  cluster_arn = {
    app       = aws_ecs_cluster.app.arn
    inference = aws_ecs_cluster.inference.arn
  }
}
