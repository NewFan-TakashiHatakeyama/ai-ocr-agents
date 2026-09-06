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

  # env で自動的に決めない。この環境は「使う時だけ up、終わったら down（destroy）」の運用で、
  # env 名が production でも実体は開発/デモ環境。env=="production" で force_destroy を
  # 外していたため、down が BucketNotEmpty で完遂できなかった（実際に踏んだ）。
  # 帳票を残したい場合のみ false にする（その場合 down 前に手で空にする必要がある）。
  force_destroy = var.s3_force_destroy

  tags = { Name = local.s3_bucket }
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

# 帳票削除（DELETE /v1/documents/{id}）は全バージョンを消すが、取りこぼしの保険。
# バージョニング有効だと旧版は明示的に消さない限り永遠に課金対象として残る。
# 未完了マルチパートアップロードも同様（一覧に出ないので気付けない）。
resource "aws_s3_bucket_lifecycle_configuration" "this" {
  bucket = aws_s3_bucket.this.id

  rule {
    id     = "expire-noncurrent-and-incomplete"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 7
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  depends_on = [aws_s3_bucket_versioning.this]
}
