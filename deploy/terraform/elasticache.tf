# ElastiCache Redis（q.extract / q.export の Streams キュー, §9）。compose の redis:7 に合わせる。
#
# 注意: Redis Streams の消費は認証付き TLS でも動くが、MVP は VPC 内クローズドのため
# transit encryption のみ有効にし AUTH は使わない（REDIS_URL に鍵を埋めない）。

resource "aws_security_group" "redis" {
  name        = "${local.prefix}-redis"
  description = "AI-OCR ElastiCache Redis"
  vpc_id      = aws_vpc.this.id
  tags        = { Name = "${local.prefix}-redis" }
}

resource "aws_vpc_security_group_ingress_rule" "redis_from_service" {
  security_group_id            = aws_security_group.redis.id
  referenced_security_group_id = aws_security_group.service.id
  from_port                    = 6379
  to_port                      = 6379
  ip_protocol                  = "tcp"
  description                  = "Redis access from ECS tasks"
}

resource "aws_elasticache_subnet_group" "this" {
  name       = "${local.prefix}-redis"
  subnet_ids = local.private_subnet_ids
  tags       = { Name = "${local.prefix}-redis" }
}

resource "aws_elasticache_replication_group" "this" {
  replication_group_id = "${local.prefix}-${var.env}"
  description          = "AI-OCR job queue (Redis Streams)"

  engine         = "redis"
  engine_version = "7.1"
  node_type      = var.redis_node_type
  port           = 6379

  # MVP は単一ノード。キューは再配信前提（§9）だが、失うと処理中ジョブが消えるため
  # 本番昇格時は num_cache_clusters=2 + automatic_failover を検討する。
  num_cache_clusters         = 1
  automatic_failover_enabled = false

  subnet_group_name  = aws_elasticache_subnet_group.this.name
  security_group_ids = [aws_security_group.redis.id]

  at_rest_encryption_enabled = true
  transit_encryption_enabled = true
  transit_encryption_mode    = "preferred"

  # Streams はメモリ上のデータが正本なので LRU で消さない（消えるとジョブが失われる）
  parameter_group_name = aws_elasticache_parameter_group.this.name

  tags = { Name = "${local.prefix}-${var.env}" }
}

resource "aws_elasticache_parameter_group" "this" {
  name        = "${local.prefix}-${var.env}"
  family      = "redis7"
  description = "AI-OCR: keep queue entries (noeviction)"

  parameter {
    name  = "maxmemory-policy"
    value = "noeviction"
  }

  tags = { Name = "${local.prefix}-${var.env}" }
}

resource "aws_secretsmanager_secret" "redis_url" {
  name        = "${local.prefix}/${var.env}/redis-url"
  description = "AI-OCR REDIS_URL"
  tags        = { Name = "${local.prefix}-redis-url" }
  # down→up を繰り返す運用のため、削除後すぐ同名で作れるようにする。既定(30日)のままだと
  # 「scheduled for deletion」で up が失敗する（実際に踏んだ）。中身は terraform が
  # 毎回生成し直すので復旧猶予に意味がない。
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "redis_url" {
  secret_id = aws_secretsmanager_secret.redis_url.id
  # transit_encryption_mode=preferred のため平文 redis:// でも接続できるが、
  # TLS を使うため rediss:// を既定にする。
  secret_string = "rediss://${aws_elasticache_replication_group.this.primary_endpoint_address}:6379/0"
}
