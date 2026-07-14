variable "aws_region" {
  type        = string
  description = "デプロイ先リージョン"
}

variable "env" {
  type        = string
  default     = "production"
  description = "環境名（タグ用）"
}

variable "image_tag" {
  type        = string
  description = "ECR イメージタグ（CD で push したもの）"
}

# --- 既存ネットワーク（本モジュールでは作成しない） ---
variable "vpc_id" {
  type        = string
  description = "既存 VPC ID"
}

variable "public_subnet_ids" {
  type        = list(string)
  description = "ALB を置く public subnet"
}

variable "private_subnet_ids" {
  type        = list(string)
  description = "ECS タスクを置く private subnet（NAT 経由で egress）"
}

# --- データストア（RDS/ElastiCache は別管理。接続文字列は Secrets Manager 参照） ---
variable "db_secret_arn" {
  type        = string
  description = "DATABASE_URL を格納した Secrets Manager ARN（postgresql+psycopg://...）"
}

variable "redis_secret_arn" {
  type        = string
  description = "REDIS_URL を格納した Secrets Manager ARN"
}

variable "jwt_secret_arn" {
  type        = string
  description = "JWT_SECRET を格納した Secrets Manager ARN"
}

variable "anthropic_secret_arn" {
  type        = string
  description = "ANTHROPIC_API_KEY を格納した Secrets Manager ARN"
}

# --- アプリ設定 ---
variable "cors_origins" {
  type        = string
  default     = ""
  description = "gateway CORS 許可オリジン（カンマ区切り）"
}

variable "structure_url" {
  type        = string
  default     = "http://structure-svc:8080"
  description = "PP-StructureV3 サービング URL（Service Connect 名）"
}

variable "llm_model" {
  type        = string
  default     = "claude-opus-4-8"
  description = "抽出/補正に使う LLM モデル ID"
}

variable "export_s3_bucket" {
  type        = string
  description = "確定 JSON / 原本の保存先 S3 バケット"
}

variable "export_s3_kms_key_id" {
  type        = string
  default     = ""
  description = "S3 SSE-KMS キー ID（任意）"
}

variable "acm_certificate_arn" {
  type        = string
  default     = ""
  description = "ALB HTTPS リスナ用 ACM 証明書 ARN（空なら HTTP のみ）"
}
