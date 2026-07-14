# キュー深度オートスケール（§13.3 / DD-14）
#
# orchestrator / structure / ocr / ingest はキュー深度でスケールする。ただしキュー基盤は
# 未確定（DD-05: Redis Streams 既定 / SQS 切替可）。そのため既定オフのトグルにして、
# SQS 採用時のみ有効化する。Redis Streams の場合はアプリが独自 CloudWatch メトリクスを
# 発行し、customized_metric_specification をそのメトリクスに差し替える。

variable "enable_sqs_queue_scaling" {
  type    = bool
  default = false
}

locals {
  # サービス → 監視する SQS キュー名（SQS 採用時のみ）
  queue_scaling = {
    orchestrator = { queue_name = "q.extract", target_backlog = 20 }
    structure    = { queue_name = "q.pages", target_backlog = 30 }
    ocr          = { queue_name = "q.pages", target_backlog = 30 }
    ingest       = { queue_name = "q.workflow", target_backlog = 20 }
  }
}

resource "aws_appautoscaling_policy" "queue_depth" {
  for_each = var.enable_sqs_queue_scaling ? local.queue_scaling : {}

  name               = "${each.key}-queue-depth"
  policy_type        = "TargetTrackingScaling"
  resource_id        = module.service[each.key].autoscaling_resource_id
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"

  target_tracking_scaling_policy_configuration {
    target_value       = each.value.target_backlog
    scale_in_cooldown  = 300
    scale_out_cooldown = 60

    customized_metric_specification {
      namespace   = "AWS/SQS"
      metric_name = "ApproximateNumberOfMessagesVisible"
      statistic   = "Average"
      dimensions {
        name  = "QueueName"
        value = each.value.queue_name
      }
    }
  }
}
