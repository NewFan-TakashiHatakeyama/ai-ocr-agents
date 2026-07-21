# ワークフロー自動トリガーの常設部（§16 設計 v0.2 §7.2 / P4）。
#
# ECR と同じ「down（本体 destroy）でも残る」長命スタックに置く理由:
#   - inbox: 顧客/連携システムが**いつでも**帳票を置ける入口。本体と一緒に消えると
#     down 中は置き場所ごと無くなり、「down 中に置く → up で自動処理」が成立しない
#   - SQS: down 中に置かれたファイルのイベントを最大 14 日バッファし、up で drain する
#
# 経路: S3(inbox, EventBridge通知) → EventBridge ルール → SQS → trigger consumer
#（orchestrator-worker 同居。up の間だけ購読）。常駐プロセスは増やさない。
#
# キー規約: inbox の最上位フォルダ = テナント ID（例: ten_1/invoices/2026-07.png）。
# イベントにはテナント情報が無いため、consumer はキーからテナントを決めて RLS 文脈を張る。

data "aws_caller_identity" "current" {}

resource "aws_s3_bucket" "inbox" {
  bucket = "ai-ocr-inbox-${data.aws_caller_identity.current.account_id}"
  # inbox は一時置き場（取込後の正本は本体バケット＋DB）。スタック破棄を残置物で
  # 妨げない。長命スタックの destroy 自体が明示的な意思決定。
  force_destroy = true
  tags          = { Name = "ai-ocr-inbox" }
}

resource "aws_s3_bucket_public_access_block" "inbox" {
  bucket                  = aws_s3_bucket.inbox.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "inbox" {
  bucket = aws_s3_bucket.inbox.id
  rule {
    apply_server_side_encryption_by_default {
      # 本体バケットは SSE-KMS だが、KMS キーは本体スタック管理で down と共に消え得る。
      # 長命の inbox はスタック間依存を作らない SSE-S3 にする（取込後の保存は本体側の KMS）。
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_notification" "inbox" {
  bucket      = aws_s3_bucket.inbox.id
  eventbridge = true
}

resource "aws_sqs_queue" "workflow_trigger_dlq" {
  name                      = "ai-ocr-workflow-trigger-dlq"
  message_retention_seconds = 1209600 # 14 日
  tags                      = { Name = "ai-ocr-workflow-trigger-dlq" }
}

resource "aws_sqs_queue" "workflow_trigger" {
  name = "ai-ocr-workflow-trigger"
  # 14 日 = down 運用（月 4 回想定）の最大バッファ。SQS の上限でもある
  message_retention_seconds  = 1209600
  # 取込は ingest（S3 書込み）+ DB 登録で数秒。余裕を見て 5 分。
  # これより長く処理が沈黙したら他 consumer へ再配信される
  visibility_timeout_seconds = 300
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.workflow_trigger_dlq.arn
    maxReceiveCount     = 5
  })
  tags = { Name = "ai-ocr-workflow-trigger" }
}

resource "aws_cloudwatch_event_rule" "inbox_object_created" {
  name        = "ai-ocr-inbox-object-created"
  description = "AI-OCR inbox S3 object created -> workflow trigger queue"
  event_pattern = jsonencode({
    source        = ["aws.s3"]
    "detail-type" = ["Object Created"]
    detail        = { bucket = { name = [aws_s3_bucket.inbox.bucket] } }
  })
}

resource "aws_cloudwatch_event_target" "inbox_to_sqs" {
  rule = aws_cloudwatch_event_rule.inbox_object_created.name
  arn  = aws_sqs_queue.workflow_trigger.arn
}

resource "aws_sqs_queue_policy" "allow_eventbridge" {
  queue_url = aws_sqs_queue.workflow_trigger.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "events.amazonaws.com" }
      Action    = "sqs:SendMessage"
      Resource  = aws_sqs_queue.workflow_trigger.arn
      Condition = { ArnEquals = { "aws:SourceArn" = aws_cloudwatch_event_rule.inbox_object_created.arn } }
    }]
  })
}

output "inbox_bucket" {
  value       = aws_s3_bucket.inbox.bucket
  description = "帳票投入用の常設バケット（最上位フォルダ = テナント ID）"
}

output "workflow_trigger_queue_url" {
  value       = aws_sqs_queue.workflow_trigger.url
  description = "trigger consumer が購読する SQS"
}
