# S3 イベント駆動トリガーの本体スタック側（§16 設計 v0.2 §7.2 / P4）。
#
# inbox バケットと SQS の実体は長命スタック（ecr/trigger.tf）にある。本体は
# down（destroy）で消えるため、ここには「参照」と「タスクロールへの権限」だけを置く。
# 名前は固定（ai-ocr-workflow-trigger / ai-ocr-inbox-<account>）なので data で引ける。
# ecr スタックが先に apply されている前提（scripts/aws_env.sh up がその順序で流す）。

data "aws_sqs_queue" "workflow_trigger" {
  name = "ai-ocr-workflow-trigger"
}

data "aws_s3_bucket" "inbox" {
  bucket = "ai-ocr-inbox-${data.aws_caller_identity.current.account_id}"
}

data "aws_iam_policy_document" "task_workflow_trigger" {
  statement {
    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:GetQueueAttributes",
      "sqs:ChangeMessageVisibility",
    ]
    resources = [data.aws_sqs_queue.workflow_trigger.arn]
  }
  statement {
    # inbox は読み取りのみ。取込後の正本は本体バケット（task_s3 の権限）に置く
    actions   = ["s3:GetObject"]
    resources = ["${data.aws_s3_bucket.inbox.arn}/*"]
  }
}

resource "aws_iam_role_policy" "task_workflow_trigger" {
  name   = "workflow-trigger"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task_workflow_trigger.json
}
