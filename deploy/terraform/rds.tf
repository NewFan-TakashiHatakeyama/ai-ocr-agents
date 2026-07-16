# RDS PostgreSQL（正本 DB, §7）。compose の postgres:16 に合わせて 16 系。
# 拡張は citext のみ（migration 0001 が CREATE EXTENSION する）。ベクトルは FAISS のため
# pgvector は不要。

resource "aws_security_group" "db" {
  name        = "${local.prefix}-db"
  description = "AI-OCR RDS PostgreSQL"
  vpc_id      = aws_vpc.this.id
  tags        = { Name = "${local.prefix}-db" }
}

# ECS タスクからのみ 5432 を許可（同一 VPC の別サービスからは開けない）
resource "aws_vpc_security_group_ingress_rule" "db_from_service" {
  security_group_id            = aws_security_group.db.id
  referenced_security_group_id = aws_security_group.service.id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
  description                  = "PostgreSQL access from ECS tasks"
}

resource "aws_db_subnet_group" "this" {
  name       = "${local.prefix}-db"
  subnet_ids = local.private_subnet_ids
  tags       = { Name = "${local.prefix}-db" }
}

resource "random_password" "db" {
  length = 32
  # RDS のマスタパスワードは / @ " space を許可しない。URL に埋めるため記号も絞る。
  override_special = "!#$%&*()-_=+[]{}<>:?"
}

resource "aws_db_instance" "this" {
  identifier = "${local.prefix}-${var.env}"
  engine     = "postgres"
  # メジャー指定（AWS が既定マイナーを選ぶ）。ap-northeast-1 の 16 系は 16.9 以降しか
  # 提供されておらず、マイナーを固定すると存在しない版を掴んで apply が落ちる（実測で確認）。
  engine_version = "16"
  instance_class = var.db_instance_class

  allocated_storage     = var.db_allocated_storage
  max_allocated_storage = var.db_allocated_storage * 4 # gp3 の自動拡張上限
  storage_type          = "gp3"
  storage_encrypted     = true

  db_name  = "newfan"
  username = "newfan"
  password = random_password.db.result

  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = [aws_security_group.db.id]
  publicly_accessible    = false
  multi_az               = var.db_multi_az

  backup_retention_period = 7

  # env で自動的に有効にしない。この環境の運用は「使う時だけ up、終わったら down（destroy）」で、
  # env 名が production でも実体は開発/デモ環境。env=="production" で有効にしていたため
  # down が deletion_protection に阻まれて完遂できなかった（実際に踏んだ）。
  # 本物の本番として常設する場合のみ true にする（その場合 down は使えない）。
  deletion_protection = var.db_deletion_protection

  # 最終スナップショットは terraform に任せない。名前が固定だと 2 回目の destroy で
  # 「既に存在する」と衝突する。scripts/aws_env.sh down が destroy 前にタイムスタンプ付きの
  # スナップショットを明示的に取る。
  skip_final_snapshot = true

  performance_insights_enabled = true
  auto_minor_version_upgrade   = true

  tags = { Name = "${local.prefix}-${var.env}" }
}

# アプリは DATABASE_URL 1 本だけを見る（§7 / worker_main は +psycopg を除去して psycopg で繋ぐ）
resource "aws_secretsmanager_secret" "database_url" {
  name        = "${local.prefix}/${var.env}/database-url"
  description = "AI-OCR DATABASE_URL (SQLAlchemy+psycopg form)"
  tags        = { Name = "${local.prefix}-database-url" }
  # down→up を繰り返す運用のため、削除後すぐ同名で作れるようにする。既定(30日)のままだと
  # 「scheduled for deletion」で up が失敗する（実際に踏んだ）。中身は terraform が
  # 毎回生成し直すので復旧猶予に意味がない。
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "database_url" {
  secret_id = aws_secretsmanager_secret.database_url.id
  secret_string = format(
    "postgresql+psycopg://%s:%s@%s/%s",
    aws_db_instance.this.username,
    urlencode(random_password.db.result),
    aws_db_instance.this.endpoint,
    aws_db_instance.this.db_name,
  )
}
