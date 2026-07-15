# JWT 署名鍵（§6.1）。RDS のパスワードと同じく本スタックが生成して Secrets Manager に置く。
# 既存の鍵を使いたい場合は var.jwt_secret_arn に ARN を渡す（その場合は作成しない）。
#
# LLM の API キーはここでは作らない。利用者自身の資格情報であり、terraform state に
# 平文で載せるべきではないため、事前に Secrets Manager へ登録して ARN を渡す方式にする。

resource "random_password" "jwt" {
  count = var.jwt_secret_arn == "" ? 1 : 0
  # HS256 の署名鍵。記号を含めると env 経由で扱いにくいので英数字のみ。
  length  = 48
  special = false
}

resource "aws_secretsmanager_secret" "jwt" {
  count       = var.jwt_secret_arn == "" ? 1 : 0
  name        = "${local.prefix}/${var.env}/jwt-secret"
  description = "AI-OCR の JWT 署名鍵"
  tags        = { Name = "${local.prefix}-jwt-secret" }
  # 作り直し（down→up）を繰り返すため、削除後すぐ同名で作れるようにする
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "jwt" {
  count         = var.jwt_secret_arn == "" ? 1 : 0
  secret_id     = aws_secretsmanager_secret.jwt[0].id
  secret_string = random_password.jwt[0].result
}
