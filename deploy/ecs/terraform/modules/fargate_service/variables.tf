variable "name" {
  type        = string
  description = "サービス名（ECS service / task family / ロググループに使用）"
}

variable "cluster_arn" {
  type        = string
  description = "配置先 ECS クラスタ ARN"
}

variable "region" {
  type = string
}

variable "cpu" {
  type    = number
  default = 512
}

variable "memory" {
  type    = number
  default = 1024
}

variable "image" {
  type        = string
  description = "ECR イメージ URI（tag 込み）"
}

variable "container_port" {
  type    = number
  default = 8000
}

variable "desired_count" {
  type    = number
  default = 1
}

variable "environment" {
  type    = map(string)
  default = {}
}

variable "secrets" {
  type        = map(string)
  description = "環境変数名 → Secrets Manager / SSM の ARN"
  default     = {}
}

variable "execution_role_arn" {
  type = string
}

variable "task_role_arn" {
  type = string
}

variable "subnet_ids" {
  type = list(string)
}

variable "security_group_ids" {
  type = list(string)
}

variable "service_connect_namespace_arn" {
  type        = string
  description = "ECS Service Connect（Cloud Map）名前空間 ARN"
}

variable "expose_via_service_connect" {
  type        = bool
  default     = true
  description = "内部サービスとして Service Connect にポートを公開するか"
}

variable "target_group_arn" {
  type        = string
  default     = null
  description = "ALB ターゲットグループ ARN（外部公開サービスのみ。内部は null）"
}

variable "health_check_path" {
  type    = string
  default = "/healthz"
}

variable "min_capacity" {
  type    = number
  default = 1
}

variable "max_capacity" {
  type    = number
  default = 4
}

variable "cpu_target_value" {
  type        = number
  default     = 60
  description = "CPU ターゲット追跡の目標使用率(%)"
}

variable "log_retention_days" {
  type    = number
  default = 30
}

variable "tags" {
  type    = map(string)
  default = {}
}
