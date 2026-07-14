data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  ecr_base   = "${local.account_id}.dkr.ecr.${var.region}.amazonaws.com"

  # 外部公開サービス（ALB 経由）。ホストベースルーティング。
  public_services = {
    gateway = { port = 8000, host = "api", priority = 10 }
    web     = { port = 3000, host = "app", priority = 20 }
  }

  # ADR-0003 Option A のサービス一覧。cluster: app | inference。
  # queue_scaled=true はキュー深度スケール対象（本モジュールでは CPU 追跡を既定とし、
  # キュー深度ポリシーは autoscaling_queue.tf で別途付与）。
  services = {
    gateway    = { cluster = "app", cpu = 512, memory = 1024, port = 8000, public = true, queue_scaled = false }
    web        = { cluster = "app", cpu = 512, memory = 1024, port = 3000, public = true, queue_scaled = false }
    orchestrator = { cluster = "app", cpu = 1024, memory = 2048, port = 8000, public = false, queue_scaled = true }
    ingest     = { cluster = "app", cpu = 1024, memory = 2048, port = 8000, public = false, queue_scaled = true }
    memory     = { cluster = "app", cpu = 512, memory = 1024, port = 8000, public = false, queue_scaled = false }
    export     = { cluster = "app", cpu = 512, memory = 1024, port = 8000, public = false, queue_scaled = false }
    "llm-adapter" = { cluster = "app", cpu = 512, memory = 1024, port = 8000, public = false, queue_scaled = false }
    # 推論（Option A: Fargate CPU / OpenVINO）。GPU 版に移す場合は Option B の別スタックへ。
    structure  = { cluster = "inference", cpu = 4096, memory = 8192, port = 8080, public = false, queue_scaled = true }
    ocr        = { cluster = "inference", cpu = 2048, memory = 4096, port = 8080, public = false, queue_scaled = true }
    # vl は Option A では未配置（GPU 必須のため。Option B で追加）
  }

  service_names = keys(local.services)
}
