output "alb_dns_name" {
  value       = aws_lb.public.dns_name
  description = "Route53 で api.<domain> / app.<domain> を向ける先"
}

output "cluster_app_name" {
  value = aws_ecs_cluster.app.name
}

output "cluster_inference_name" {
  value = aws_ecs_cluster.inference.name
}

output "ecr_repository_urls" {
  value = { for k, r in aws_ecr_repository.svc : k => r.repository_url }
}

output "service_connect_namespace" {
  value = aws_service_discovery_http_namespace.internal.name
}
