# ECR だけを持つ独立スタック（長命）。
#
# 本体（deploy/terraform）は「使う時だけ up、終わったら down（destroy）」で作り直すが、
# ECR は消したくない。イメージが 30GB あり、消すと次回の up で再ビルド＋再 push に
# 30 分以上かかるため。同一 state に置くと destroy が
# RepositoryNotEmptyException で失敗し、down 全体が完遂しなかった（実際に踏んだ）。
#
# 使い方（scripts/aws_env.sh up が自動で流す。冪等なので何度実行してもよい）:
#   cd deploy/terraform/ecr && terraform init && terraform apply
#
# 作り直したい場合のみ: terraform destroy（イメージも消えるので通常は不要）

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.40"
    }
  }
}

provider "aws" {
  region = var.aws_region
  default_tags {
    tags = {
      Service   = "ai-ocr"
      Project   = "newfan-ai-ocr"
      ManagedBy = "terraform"
      Repo      = "ai-ocr-agents"
    }
  }
}

variable "aws_region" {
  type        = string
  default     = "ap-northeast-1"
  description = "リージョン"
}

variable "name_prefix" {
  type        = string
  default     = "ai-ocr"
  description = "リポジトリ名の接頭辞（本体スタックの name_prefix と一致させること）"
}

locals {
  # 本体の local.images と一致させること。食い違うと本体の apply が
  # 「data source が見つからない」で失敗する。
  images = [
    "gateway", "orchestrator-worker", "export-worker", "migrate",
    "inference", "web", "vl-pipeline", "vlm-server",
  ]
}

resource "aws_ecr_repository" "this" {
  for_each             = toset(local.images)
  name                 = "${var.name_prefix}-${each.key}"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration {
    scan_on_push = true
  }
  tags = { Name = "${var.name_prefix}-${each.key}" }
}

# 推論/VL イメージは 5〜22GB。世代を残すと保管料が効くため絞る。
resource "aws_ecr_lifecycle_policy" "this" {
  for_each   = aws_ecr_repository.this
  repository = each.value.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "keep only the 5 most recent images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 5 }
      action       = { type = "expire" }
    }]
  })
}

output "repository_urls" {
  value       = { for k, r in aws_ecr_repository.this : k => r.repository_url }
  description = "各イメージの ECR URL"
}
