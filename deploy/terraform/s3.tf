# 原本 PNG / ページ画像 / 確定 JSON の保管先（§2.3 パス規約: {tenant}/{document}/...）。
#
# gateway(ingest) が書き、orchestrator-worker が s3:// で読み、export-worker が確定 JSON を書く。
# 3 者が別タスクなので共有ストレージが必須。S3_BUCKET を渡さないと gateway は
# LocalObjectStore になり file:///app/data/... を返す → タスクのエフェメラルディスクは
# 共有されないため orchestrator から読めず抽出が失敗する（ローカルで同種の失敗を踏んだ）。

data "aws_caller_identity" "current" {}

locals {
  # バケット名はグローバル一意。アカウント ID を付けて衝突を避ける。
  s3_bucket = var.s3_bucket_name != "" ? var.s3_bucket_name : "${local.prefix}-${var.env}-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket" "this" {
  bucket = local.s3_bucket
  # 開発環境は down（destroy）で作り直す運用のため、中身ごと消せるようにする。
  force_destroy = var.env != "production"
  tags          = { Name = local.s3_bucket }
}

resource "aws_s3_bucket_public_access_block" "this" {
  bucket                  = aws_s3_bucket.this.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# S3ObjectStore.put は KMS キー未指定でも常に SSE-KMS で PUT する（aws/s3 マネージドキー）。
# バケット既定も KMS に揃えておく。
resource "aws_s3_bucket_server_side_encryption_configuration" "this" {
  bucket = aws_s3_bucket.this.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.s3_kms_key_id != "" ? var.s3_kms_key_id : null
    }
    bucket_key_enabled = true # KMS リクエスト料を抑える
  }
}

resource "aws_s3_bucket_versioning" "this" {
  bucket = aws_s3_bucket.this.id
  versioning_configuration {
    status = var.env == "production" ? "Enabled" : "Disabled"
  }
}
