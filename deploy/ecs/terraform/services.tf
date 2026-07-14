# 各サービスを fargate_service モジュールで生成（ADR-0003 Option A）

module "service" {
  source   = "./modules/fargate_service"
  for_each = local.services

  name        = each.key
  cluster_arn = local.cluster_arn[each.value.cluster]
  region      = var.region

  cpu            = each.value.cpu
  memory         = each.value.memory
  container_port = each.value.port
  image          = "${local.ecr_base}/${var.project}/${each.key}:${var.image_tag}"

  execution_role_arn = aws_iam_role.execution.arn
  task_role_arn      = aws_iam_role.task.arn

  subnet_ids         = var.private_subnet_ids
  security_group_ids = [aws_security_group.tasks.id]

  service_connect_namespace_arn = aws_service_discovery_http_namespace.internal.arn

  # 外部公開サービスは ALB ターゲットグループに紐付け
  target_group_arn = each.value.public ? aws_lb_target_group.public[each.key].arn : null

  environment = {
    APP_ENV    = var.env
    AWS_REGION = var.region
  }
  secrets = {
    DATABASE_URL = var.db_secret_arn
  }

  # キュー深度スケール対象は最大数を大きめに（キュー深度ポリシーは autoscaling_queue.tf）
  min_capacity = 1
  max_capacity = each.value.queue_scaled ? 10 : 4

  tags = { Service = each.key }

  depends_on = [aws_lb_listener.https]
}
