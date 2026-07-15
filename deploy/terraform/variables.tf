variable "aws_region" {
  type        = string
  description = "デプロイ先リージョン"
}

variable "env" {
  type        = string
  default     = "production"
  description = "環境名（タグ・リソース名に入る）"
}

# 同一アカウントに GraphSuite 等の別サービスが同居するため、AWS コンソール上で
# 一目で AI-OCR のリソースと分かる名前にする（既存の graphsuite-api / graphsuite-dev と
# 同じ <サービス>-<コンポーネント> 規則に合わせる）。
variable "name_prefix" {
  type        = string
  default     = "ai-ocr"
  description = "全リソース名の接頭辞。何のサービスか判別できるよう ai-ocr を含める"

  validation {
    condition     = can(regex("ai-ocr", lower(var.name_prefix)))
    error_message = "name_prefix には ai-ocr を含めてください（別サービスのリソースと区別するため）。"
  }
}

variable "image_tag" {
  type        = string
  description = "ECR イメージタグ（CD で push したもの）"
}

# --- ネットワーク（本スタックが専用 VPC を作成する。vpc.tf 参照） ---
# 既定 VPC には NAT が無く、他プロジェクト(GraphSuite/AIReadyConnect)と混在するため
# AI-OCR 専用 VPC を持つ。
variable "vpc_cidr" {
  type        = string
  default     = "10.1.0.0/16"
  description = "AI-OCR VPC の CIDR（既存の 172.31.0.0/16 と 10.0.0.0/16 に重ねないこと）"
}

# --- データストア（本スタックが作成し、接続文字列を Secrets Manager に格納する） ---
variable "db_instance_class" {
  type        = string
  default     = "db.t4g.medium"
  description = "RDS PostgreSQL のインスタンスクラス"
}

variable "db_allocated_storage" {
  type        = number
  default     = 50
  description = "RDS の割当ストレージ(GB)。gp3 で自動拡張する"
}

variable "db_multi_az" {
  type        = bool
  default     = false
  description = "RDS Multi-AZ（MVP は単一 AZ。本番昇格時に true）"
}

variable "redis_node_type" {
  type        = string
  default     = "cache.t4g.small"
  description = "ElastiCache(Redis) のノードタイプ"
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

# 推論サービングは Service Connect の client_alias で名前解決する（service_connect.tf）。
# URL を変数で受けると alias と食い違って解決不能になるため、locals で固定する。

variable "structure_cpu" {
  type        = number
  default     = 4096
  description = "structure-svc の vCPU(1024=1vCPU)。実測: 4vCPU=16.4s/枚, 8vCPU=11.6s/枚"
}

variable "structure_memory" {
  type        = number
  default     = 8192
  description = "structure-svc のメモリ(MiB)"
}

variable "structure_seal_enabled" {
  type        = bool
  default     = false
  description = "印章認識オプション。true で印章版 config のイメージを使う（DD-03）"
}

variable "ocr_cpu" {
  type        = number
  default     = 2048
  description = "ocr-svc の vCPU（DD-02 char_backfill 用の /ocr 単体）"
}

variable "ocr_memory" {
  type        = number
  default     = 4096
  description = "ocr-svc のメモリ(MiB)"
}

variable "gemini_secret_arn" {
  type        = string
  default     = ""
  description = "GEMINI_API_KEY の Secrets Manager ARN（LLM_PROVIDER=gemini 時。空なら未設定）"
}

variable "llm_provider" {
  type        = string
  default     = "anthropic"
  description = "LLM プロバイダ（anthropic | gemini）"

  validation {
    condition     = contains(["anthropic", "gemini"], var.llm_provider)
    error_message = "llm_provider は anthropic か gemini。"
  }
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
