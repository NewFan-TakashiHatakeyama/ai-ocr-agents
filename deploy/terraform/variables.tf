variable "aws_region" {
  type        = string
  description = "デプロイ先リージョン"
}

variable "env" {
  type        = string
  default     = "production"
  description = "環境名（タグ・リソース名に入る）"
}

# 同一アカウントに GraphSuite 等の別サービスが同居するため、AWS コンソール上で
# 一目で AI-OCR のリソースと分かる名前にする（既存の graphsuite-api / graphsuite-dev と
# 同じ <サービス>-<コンポーネント> 規則に合わせる）。
variable "name_prefix" {
  type        = string
  default     = "ai-ocr"
  description = "全リソース名の接頭辞。何のサービスか判別できるよう ai-ocr を含める"

  validation {
    condition     = can(regex("ai-ocr", lower(var.name_prefix)))
    error_message = "name_prefix には ai-ocr を含めてください（別サービスのリソースと区別するため）。"
  }
}

variable "image_tag" {
  type        = string
  description = "ECR イメージタグ（CD で push したもの）"
}

# --- ネットワーク（本スタックが専用 VPC を作成する。vpc.tf 参照） ---
# 既定 VPC には NAT が無く、他プロジェクト(GraphSuite/AIReadyConnect)と混在するため
# AI-OCR 専用 VPC を持つ。
variable "vpc_cidr" {
  type        = string
  default     = "10.1.0.0/16"
  description = "AI-OCR VPC の CIDR（既存の 172.31.0.0/16 と 10.0.0.0/16 に重ねないこと）"
}

# --- 起動/停止 ---
# 月に数回しか使わない環境では「立てっぱなし」がそのまま無駄になる。止め方は 2 段階:
#
#  1) services_enabled=false → Fargate を全て 0 台に。数十秒で戻せ、DB もエンドポイントも
#     残る。ただし ElastiCache / NAT / ALB は AWS 側に「停止」が無く削除しかないため、
#     RDS+Redis+NAT+ALB+ストレージ の約 $189/月 は残る。「今夜は使わない」用。
#  2) terraform destroy → 残るのは ECR($3) と DB スナップショット($1) だけ。復旧は 15-20 分
#     （RDS 作成が支配的）。月数回の利用ならこちらが既定。data は db_snapshot_identifier で戻す。
#
# 起動中の実費は $0.766/h（Fargate $0.529 + RDS/Redis/NAT/ALB $0.236）。
variable "services_enabled" {
  type        = bool
  default     = true
  description = "false で全 ECS サービスを 0 台にする（インフラは残す。完全に止めるなら destroy）"
}

variable "container_insights_enabled" {
  type        = bool
  default     = true
  description = "Container Insights（約 $18/月）。開発環境では false 推奨"
}

# --- データストア（本スタックが作成し、接続文字列を Secrets Manager に格納する） ---
#
# destroy するとデータは消える（開発/デモ環境の前提）。残したい場合は destroy 前に
# scripts/aws_env.sh が手動スナップショットを取る。復元は明示的な手作業とする
# （terraform の snapshot_identifier はマスタパスワードの扱いが分かりにくく、
#  復元時に DATABASE_URL と食い違って接続不能になりうるため、あえて自動化しない）。
variable "db_instance_class" {
  type        = string
  default     = "db.t4g.medium"
  description = "RDS PostgreSQL のインスタンスクラス"
}

variable "db_allocated_storage" {
  type        = number
  default     = 50
  description = "RDS の割当ストレージ(GB)。gp3 で自動拡張する"
}

variable "db_multi_az" {
  type        = bool
  default     = false
  description = "RDS Multi-AZ（MVP は単一 AZ。本番昇格時に true）"
}

variable "db_deletion_protection" {
  type        = bool
  default     = false
  description = "RDS の削除保護。既定 false（down=destroy の運用のため）。常設する場合のみ true"
}

variable "redis_node_type" {
  type        = string
  default     = "cache.t4g.small"
  description = "ElastiCache(Redis) のノードタイプ"
}

variable "jwt_secret_arn" {
  type        = string
  default     = ""
  description = "既存の JWT 署名鍵の ARN。空なら terraform が生成する（secrets.tf）"
}

# LLM の API キーは利用者自身の資格情報のため terraform では作らない。
# 事前に Secrets Manager へ登録し、ARN をここに渡す。
variable "anthropic_secret_arn" {
  type        = string
  default     = ""
  description = "ANTHROPIC_API_KEY の Secrets Manager ARN（llm_provider=anthropic なら必須）"
}

# --- アプリ設定 ---
variable "cors_origins" {
  type        = string
  default     = ""
  description = "gateway CORS 許可オリジン（カンマ区切り）"
}

# --- 読取領域ヒント（orchestrator-worker。設計 region-field-add-and-hint-v2 §1.5 / §2.10） ---
# 第 3 回計測（2026-09-12）で出荷ゲートを通し既定 on。ここは**キルスイッチ**で、
# 止めるときだけ "0" を渡す（未設定・空文字は on。"0" / "false" / "no" / "off" だけが off）。
# 既定 "" で apply すると環境変数は空文字で渡り、worker は on と解釈する。
variable "region_kie_hints" {
  type        = string
  default     = ""
  description = "読取領域ヒントのキルスイッチ。未設定・空は on、\"0\" で止める"
}

# 「有効化前に引かれた領域」注記の境界。コードの定数（2026-09-12T00:00:00Z）が既定で、
# この環境で実際に on にした時点が違う（止めていた期間がある等）ときだけ渡す。
# 形式の誤記は plan の時点で止める。形式は合うが worker が解釈できない値（存在しない
# 日付等）は worker が warning を出して定数に倒す（タスクは落ちない）。
#
# 時刻と tz（Z かオフセット）まで必須。worker は tz の無い値（日付だけ等）を UTC とみなす
# ので、「on にした日（JST）」のつもりで 2026-10-01 と書くと境界が 2026-10-01T00:00:00Z
# ＝ JST 09:00 になり、その日の 0〜9 時に引かれた（作者が確認済みの）領域に注記が出る。
# JST の時点は 2026-10-01T00:00:00+09:00 のように書く。
variable "region_hints_activated_at" {
  type        = string
  default     = ""
  description = "読取領域ヒントを有効化した時点（ISO 8601。時刻と Z かオフセットまで必須。日付だけは不可）。空はコードの定数 2026-09-12T00:00:00Z"

  validation {
    condition     = var.region_hints_activated_at == "" || can(regex("^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}(:\\d{2}(\\.\\d+)?)?(Z|[+-]\\d{2}:\\d{2})$", var.region_hints_activated_at))
    error_message = "region_hints_activated_at は時刻と tz まで書いた ISO 8601（例: 2026-09-12T00:00:00Z / 2026-09-12T09:00:00+09:00）か空文字。日付だけ（2026-09-12）や tz 無しは不可 ── worker が UTC とみなして JST と 9 時間ずれるため。"
  }
}

# 位置ずれ検知（読取領域と違う位置で項目が見つかった）を実際にレビューへ反映するか
# （設計 region-template-editor D10 / Phase 5）。既定は shadow（metrics とログのみ）。
# "1" / "true" / "yes" / "on" で有効化。有効化の判断は shadow 実測（誤検知率）に基づく。
variable "region_guard_enforce" {
  type        = string
  default     = ""
  description = "位置ガードをレビューへ反映する（Phase 5）。空は shadow、\"1\" で有効化"
}

# 別レイアウトと判定したページで除外領域を見送るか（設計 §5.4 / §11-8）。既定 off。
# on にすると、読取領域の例示値がそのページに 1 つも見つからないとき除外を適用せず、
# metrics.region.skipped_exclude_pages に記録する（検証画面にバッジが出る）。
variable "region_exclude_skip_on_layout_mismatch" {
  type        = string
  default     = ""
  description = "別レイアウトと判定したページで除外領域を見送る。空は off、\"1\" で有効化"
}

# 推論サービングは Service Connect の client_alias で名前解決する（service_connect.tf）。
# URL を変数で受けると alias と食い違って解決不能になるため、locals で固定する。

variable "structure_cpu" {
  type        = number
  default     = 4096
  description = "structure-svc の vCPU(1024=1vCPU)。実測: 4vCPU=16.4s/枚, 8vCPU=11.6s/枚"
}

variable "structure_memory" {
  type        = number
  default     = 8192
  description = "structure-svc のメモリ(MiB)"
}

variable "structure_seal_enabled" {
  type        = bool
  default     = false
  description = "印章認識オプション。true で印章版 config のイメージを使う（DD-03）"
}

variable "ocr_cpu" {
  type        = number
  default     = 2048
  description = "ocr-svc の vCPU（DD-02 char_backfill 用の /ocr 単体）"
}

variable "ocr_memory" {
  type        = number
  default     = 4096
  description = "ocr-svc のメモリ(MiB)"
}

# --- VL フォールバック（§5.4 / DD-09。GPU が要るのは vlm-server だけ） ---
# 既定 false = GPU インスタンス 0 台で課金なし。使う時だけ scripts/aws_env.sh vl-up。
# 実価格 g4dn.xlarge OnDemand $0.710/h。常時起動は $528/月でコストオーバーのため既定オフ。
variable "vl_enabled" {
  type        = bool
  default     = false
  description = "VL フォールバックを有効にする（GPU インスタンスが起動し課金される）"
}

variable "vl_instance_type" {
  type        = string
  default     = "g4dn.xlarge"
  description = "vlm-server の GPU インスタンス（T4 16GB。実価格 $0.710/h）"
}

variable "vl_disk_gb" {
  type        = number
  default     = 120
  description = "GPU インスタンスの EBS(GB)。vlm-server イメージが 18.3GB あるため既定 30GB では不足"
}

variable "vl_cpu" {
  type        = number
  default     = 2048
  description = "vl-svc（パイプライン, CPU Fargate）の vCPU"
}

variable "vl_memory" {
  type        = number
  default     = 4096
  description = "vl-svc のメモリ(MiB)"
}

variable "gemini_secret_arn" {
  type        = string
  default     = ""
  description = "GEMINI_API_KEY の Secrets Manager ARN（LLM_PROVIDER=gemini 時。空なら未設定）"
}

variable "llm_provider" {
  type        = string
  default     = "anthropic"
  description = "LLM プロバイダ（anthropic | gemini）"

  validation {
    condition     = contains(["anthropic", "gemini"], var.llm_provider)
    error_message = "llm_provider は anthropic か gemini。"
  }

  # キーが無いまま apply すると、タスクは起動したあと provider 生成で落ちる。
  # 気付きにくいので plan の時点で止める。
  validation {
    condition     = var.llm_provider != "anthropic" || var.anthropic_secret_arn != ""
    error_message = "llm_provider=anthropic には anthropic_secret_arn が必要です。"
  }
  validation {
    condition     = var.llm_provider != "gemini" || var.gemini_secret_arn != ""
    error_message = "llm_provider=gemini には gemini_secret_arn が必要です。"
  }
}

variable "llm_model" {
  type        = string
  default     = "claude-opus-4-8"
  description = "抽出/補正に使う LLM モデル ID"
}

# 原本/ページ画像/確定JSON を置く単一バケット（s3.tf が作成する）。
# gateway・orchestrator-worker・export-worker が同じバケットを共有する。
variable "s3_bucket_name" {
  type        = string
  default     = ""
  description = "S3 バケット名。空なら ai-ocr-<env>-<accountid> を自動採番する"
}

variable "s3_force_destroy" {
  type        = bool
  default     = true
  description = "down（destroy）でバケットを中身ごと消す。既定 true（月数回の利用を前提とした運用）"
}

variable "s3_kms_key_id" {
  type        = string
  default     = ""
  description = "SSE-KMS のキー ID（空なら AWS マネージドキー aws/s3 を使う）"
}

variable "acm_certificate_arn" {
  type        = string
  default     = ""
  description = "ALB HTTPS リスナ用 ACM 証明書 ARN（空なら HTTP のみ）"
}

# 取込の前処理（DD-01 / ADR-0002 追記）。空・"none"（既定）は前処理なし、"deskew" で
# 射影プロファイルの傾き補正（gateway の手動アップロードと worker の自動取込の両方に効く）。
variable "ingest_preprocess" {
  type        = string
  default     = ""
  description = "取込の前処理。空は none、\"deskew\" で傾き補正"
  validation {
    condition     = contains(["", "none", "deskew"], var.ingest_preprocess)
    error_message = "ingest_preprocess は \"\" / \"none\" / \"deskew\" のいずれか。"
  }
}
