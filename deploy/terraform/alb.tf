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

  # 推論サービング（structure-svc / ocr-svc）は同じ SG のタスクとして起動する。
  # SG 内の通信は既定で不許可のため、自己参照を入れないと orchestrator-worker から
  # http://structure-svc:8080 に到達できない（Service Connect でも宛先ポートは要開放）。
  ingress {
    description = "推論サービングへの内部通信（Service Connect）"
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

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.gateway.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.gateway.arn
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

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.gateway.arn
  }
}
