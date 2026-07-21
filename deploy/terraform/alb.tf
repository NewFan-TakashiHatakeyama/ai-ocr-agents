# gateway 用 ALB（外部公開は gateway のみ）。WAF アタッチは別途推奨。

resource "aws_security_group" "alb" {
  name        = "${local.prefix}-alb"
  description = "AI-OCR gateway ALB ingress"
  vpc_id      = aws_vpc.this.id
  tags        = { Name = "${local.prefix}-alb" }

  ingress {
    description = "HTTP"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    description = "HTTPS"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "service" {
  name        = "${local.prefix}-service"
  description = "AI-OCR ECS tasks"
  vpc_id      = aws_vpc.this.id
  tags        = { Name = "${local.prefix}-service" }

  ingress {
    description     = "gateway from ALB"
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  ingress {
    description     = "web from ALB"
    from_port       = 3000
    to_port         = 3000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  # 推論サービング（structure-svc / ocr-svc）は同じ SG のタスクとして起動する。
  # SG 内の通信は既定で不許可のため、自己参照を入れないと orchestrator-worker から
  # http://structure-svc:8080 に到達できない（Service Connect でも宛先ポートは要開放）。
  ingress {
    description = "internal traffic to inference serving (Service Connect)"
    from_port   = 8080
    to_port     = 8080
    protocol    = "tcp"
    self        = true
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_lb" "gateway" {
  name               = "${local.prefix}-gateway"
  load_balancer_type = "application"
  subnets            = local.public_subnet_ids
  security_groups    = [aws_security_group.alb.id]
  tags               = { Name = "${local.prefix}-gateway" }
}

resource "aws_lb_target_group" "gateway" {
  name        = "${local.prefix}-gateway"
  port        = 8000
  protocol    = "HTTP"
  vpc_id      = aws_vpc.this.id
  target_type = "ip"

  health_check {
    path                = "/healthz"
    matcher             = "200"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

# web（Next.js）。gateway と同じ ALB に相乗りさせ、/v1/* だけ gateway へ振る。
# 同一オリジンになるのでブラウザから見て CORS が不要になり、web に焼き込む
# NEXT_PUBLIC_API_BASE も ALB の URL 一本で済む。
resource "aws_lb_target_group" "web" {
  name        = "${local.prefix}-web"
  port        = 3000
  protocol    = "HTTP"
  vpc_id      = aws_vpc.this.id
  target_type = "ip"

  health_check {
    path = "/"
    # Next.js の / は /dashboard へ 307 リダイレクトするため 200 固定だと落ちる
    matcher             = "200-399"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  tags = { Name = "${local.prefix}-web" }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.gateway.arn
  port              = 80
  protocol          = "HTTP"

  # 証明書があれば HTTP は HTTPS へ寄せる。JWT を Authorization ヘッダで送るため、
  # 平文の 80 を残すとトークンが盗聴されうる（証明書が無い開発時のみ 80 で受ける）。
  dynamic "default_action" {
    for_each = var.acm_certificate_arn == "" ? [] : [1]
    content {
      type = "redirect"
      redirect {
        port        = "443"
        protocol    = "HTTPS"
        status_code = "HTTP_301"
      }
    }
  }

  # 既定は web。API だけをルールで gateway に振る。
  dynamic "default_action" {
    for_each = var.acm_certificate_arn == "" ? [1] : []
    content {
      type             = "forward"
      target_group_arn = aws_lb_target_group.web.arn
    }
  }
}

# HTTP(80) の API ルール。HTTPS へリダイレクトする構成では 80 にルールを付けない
# （付けると API だけ平文で通ってしまい、リダイレクトの意味が無くなる）。
resource "aws_lb_listener_rule" "api" {
  count        = var.acm_certificate_arn == "" ? 1 : 0
  listener_arn = aws_lb_listener.http.arn
  priority     = 10

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.gateway.arn
  }
  condition {
    path_pattern {
      values = ["/v1/*", "/healthz"]
    }
  }
}

# HTTPS(443) の API ルール。80 と同じ振り分けを 443 にも用意する
# （これが無いと HTTPS では全部 web に行き、API が 404 になる）。
resource "aws_lb_listener_rule" "api_https" {
  count        = var.acm_certificate_arn == "" ? 0 : 1
  listener_arn = aws_lb_listener.https[0].arn
  priority     = 10

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.gateway.arn
  }
  condition {
    path_pattern {
      values = ["/v1/*", "/healthz"]
    }
  }
}

# ACM 証明書がある場合のみ HTTPS:443 を追加。
resource "aws_lb_listener" "https" {
  count             = var.acm_certificate_arn == "" ? 0 : 1
  load_balancer_arn = aws_lb.gateway.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.acm_certificate_arn

  # 既定は web（80 と同じ）。API は aws_lb_listener_rule.api_https で振る。
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.web.arn
  }
}
