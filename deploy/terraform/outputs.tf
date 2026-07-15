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

output "db_endpoint" {
  value       = aws_db_instance.this.endpoint
  description = "RDS PostgreSQL のエンドポイント（接続文字列は Secrets Manager 参照）"
}

output "redis_endpoint" {
  value       = aws_elasticache_replication_group.this.primary_endpoint_address
  description = "ElastiCache Redis のプライマリエンドポイント"
}

output "database_url_secret_arn" {
  value       = aws_secretsmanager_secret.database_url.arn
  description = "DATABASE_URL の Secrets Manager ARN（migrate の run-task で使う）"
}

output "service_security_group_id" {
  value       = aws_security_group.service.id
  description = "ECS タスクの SG（migrate の run-task で指定する）"
}

output "inference_urls" {
  value       = { structure = local.structure_url, ocr = local.ocr_url }
  description = "Service Connect で解決される推論サービング URL"
}

output "s3_bucket" {
  value       = aws_s3_bucket.this.id
  description = "原本/ページ画像/確定JSON のバケット"
}

output "vl_enabled" {
  value       = var.vl_enabled
  description = "VL フォールバックが有効か（true なら GPU インスタンスが課金される）"
}
