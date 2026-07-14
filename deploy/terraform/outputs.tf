output "alb_dns_name" {
  value       = aws_lb.gateway.dns_name
  description = "gateway ALB の DNS 名（Route53 CNAME 先）"
}

output "ecr_repository_urls" {
  value       = { for k, r in aws_ecr_repository.this : k => r.repository_url }
  description = "各サービスの ECR リポジトリ URL"
}

output "cluster_name" {
  value       = aws_ecs_cluster.app.name
  description = "ECS クラスタ名"
}

output "migrate_task_definition_arn" {
  value       = aws_ecs_task_definition.migrate.arn
  description = "run-task で実行するマイグレーションタスク定義"
}
