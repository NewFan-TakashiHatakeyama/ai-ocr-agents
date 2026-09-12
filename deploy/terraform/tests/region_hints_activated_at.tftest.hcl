# region_hints_activated_at の validation（variables.tf）を固定する。
#
# worker は tz の無い値（日付だけ等）を UTC とみなすので、「on にした日（JST）」のつもりで
# 日付だけを書くと境界が 9 時間ずれる。それを plan の時点で弾くのがこの validation の役目。
# 実 AWS には触らない（provider は mock。variables の validation だけを見る）。
#
# 実行: cd deploy/terraform && terraform init -backend=false && terraform test

mock_provider "aws" {
  # mock の既定値では形が合わない data source（IAM ポリシーは JSON オブジェクト、AZ は 2 つ以上）
  mock_data "aws_iam_policy_document" {
    defaults = {
      json = jsonencode({ Version = "2012-10-17", Statement = [] })
    }
  }
  mock_data "aws_availability_zones" {
    defaults = {
      names = ["ap-northeast-1a", "ap-northeast-1c", "ap-northeast-1d"]
    }
  }
}
mock_provider "random" {}

variables {
  aws_region           = "ap-northeast-1"
  image_tag            = "test"
  anthropic_secret_arn = "arn:aws:secretsmanager:ap-northeast-1:123456789012:secret:anthropic-test"
}

run "empty_is_default" {
  command = plan
  variables {
    region_hints_activated_at = ""
  }
}

run "utc_z" {
  command = plan
  variables {
    region_hints_activated_at = "2026-09-12T00:00:00Z"
  }
}

run "offset_jst" {
  command = plan
  variables {
    region_hints_activated_at = "2026-10-01T00:00:00+09:00"
  }
}

run "minutes_only" {
  command = plan
  variables {
    region_hints_activated_at = "2026-10-01T09:00+09:00"
  }
}

run "fraction_seconds" {
  command = plan
  variables {
    region_hints_activated_at = "2026-10-01T00:00:00.5Z"
  }
}

# 日付だけは弾く（worker が UTC 00:00 とみなし、JST の 0 時と 9 時間ずれるため）
run "date_only_rejected" {
  command = plan
  variables {
    region_hints_activated_at = "2026-09-12"
  }
  expect_failures = [var.region_hints_activated_at]
}

# 時刻はあるが tz が無いものも弾く
run "naive_datetime_rejected" {
  command = plan
  variables {
    region_hints_activated_at = "2026-10-01T00:00:00"
  }
  expect_failures = [var.region_hints_activated_at]
}

run "garbage_rejected" {
  command = plan
  variables {
    region_hints_activated_at = "2026/10/01"
  }
  expect_failures = [var.region_hints_activated_at]
}
