terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.40"
    }
    # RDS マスタパスワード生成（rds.tf）
    random = {
      source  = "hashicorp/random"
      version = ">= 3.5"
    }
  }
}

provider "aws" {
  region = var.aws_region

  # 同一アカウントに GraphSuite 等が同居する（既存リソースはタグ無しで運用されている）。
  # コスト配分・棚卸し・誤操作防止のため、AI-OCR のリソースは全て Service=ai-ocr で
  # 引ける状態にする。Name タグは各リソース側で個別に付ける（コンソールの表示名）。
  default_tags {
    tags = {
      Service   = "ai-ocr"
      Project   = "newfan-ai-ocr"
      Env       = var.env
      ManagedBy = "terraform"
      Repo      = "ai-ocr-agents"
    }
  }
}
