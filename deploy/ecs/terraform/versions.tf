terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # backend は環境ごとに設定（例: S3 + DynamoDB ロック）。
  # backend "s3" {
  #   bucket         = "newfan-tfstate"
  #   key            = "ecs/terraform.tfstate"
  #   region         = "ap-northeast-1"
  #   dynamodb_table = "newfan-tflock"
  #   encrypt        = true
  # }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      Project   = var.project
      ManagedBy = "terraform"
      Env       = var.env
    }
  }
}
