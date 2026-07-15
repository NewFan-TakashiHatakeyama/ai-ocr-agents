# AI-OCR 専用 VPC。
#
# 同一アカウントには GraphSuite / AIReadyConnect(PoC) が同居しており、既定 VPC には
# NAT が無い。相乗りするとサービス境界・課金・SG が混ざるため専用 VPC を持つ。
# CIDR は既存と重ならないものを選ぶ（default=172.31.0.0/16, AIReadyConnect=10.0.0.0/16）。
#
# egress が必須な理由: ECR からのイメージ pull（推論は 4.8GB）、Secrets Manager、
# CloudWatch Logs、そして Gemini/Anthropic の API 呼び出し。private subnet に置くため
# NAT Gateway が要る。コスト（ADR-0003）優先で NAT は 1 台だけにする。

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true # Service Connect / RDS のエンドポイント解決に必要
  tags                 = { Name = "${local.prefix}-vpc" }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = "${local.prefix}-igw" }
}

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  # RDS/ElastiCache のサブネットグループは 2AZ 以上が必須のため 2 AZ に分ける。
  azs = slice(data.aws_availability_zones.available.names, 0, 2)

  public_subnet_ids  = [for s in aws_subnet.public : s.id]
  private_subnet_ids = [for s in aws_subnet.private : s.id]
}

# --- public（ALB と NAT のみ） ---
resource "aws_subnet" "public" {
  for_each                = { for i, az in local.azs : az => i }
  vpc_id                  = aws_vpc.this.id
  availability_zone       = each.key
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, each.value)
  map_public_ip_on_launch = false
  tags                    = { Name = "${local.prefix}-public-${each.key}" }
}

# --- private（ECS タスク / RDS / ElastiCache） ---
resource "aws_subnet" "private" {
  for_each          = { for i, az in local.azs : az => i }
  vpc_id            = aws_vpc.this.id
  availability_zone = each.key
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, each.value + 10)
  tags              = { Name = "${local.prefix}-private-${each.key}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }
  tags = { Name = "${local.prefix}-public" }
}

resource "aws_route_table_association" "public" {
  for_each       = aws_subnet.public
  subnet_id      = each.value.id
  route_table_id = aws_route_table.public.id
}

# --- NAT（1 台のみ。AZ 障害時は private の egress が止まるが MVP はコスト優先） ---
resource "aws_eip" "nat" {
  domain = "vpc"
  tags   = { Name = "${local.prefix}-nat" }
}

resource "aws_nat_gateway" "this" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.public[local.azs[0]].id
  tags          = { Name = "${local.prefix}-nat" }
  depends_on    = [aws_internet_gateway.this]
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.this.id
  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.this.id
  }
  tags = { Name = "${local.prefix}-private" }
}

resource "aws_route_table_association" "private" {
  for_each       = aws_subnet.private
  subnet_id      = each.value.id
  route_table_id = aws_route_table.private.id
}

# --- S3 は Gateway エンドポイント（無料）で NAT を経由させない ---
# 原本 PNG / 確定 JSON の read/write は S3 に集中するため、NAT のデータ処理料を避ける。
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${var.aws_region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id]
  tags              = { Name = "${local.prefix}-s3" }
}
