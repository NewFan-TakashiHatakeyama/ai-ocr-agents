variable "project" {
  type    = string
  default = "newfan-ocr"
}

variable "env" {
  type    = string
  default = "production"
}

variable "region" {
  type    = string
  default = "ap-northeast-1"
}

# 既存 VPC を利用（VPC 自体は別スタック/別管理を想定）
variable "vpc_id" {
  type        = string
  description = "配置先 VPC ID"
}

variable "private_subnet_ids" {
  type        = list(string)
  description = "ECS タスク配置用のプライベートサブネット"
}

variable "public_subnet_ids" {
  type        = list(string)
  description = "ALB 配置用のパブリックサブネット"
}

variable "acm_certificate_arn" {
  type        = string
  description = "ALB HTTPS リスナー用の ACM 証明書 ARN"
}

variable "image_tag" {
  type        = string
  default     = "latest"
  description = "各サービスの共通イメージタグ（CI が push した tag）"
}

variable "db_secret_arn" {
  type        = string
  description = "DATABASE_URL を格納した Secrets Manager シークレットの ARN"
}

variable "allowed_ingress_cidrs" {
  type        = list(string)
  default     = ["0.0.0.0/0"]
  description = "ALB への許可 CIDR（本番は WAF＋必要に応じ絞る）"
}
