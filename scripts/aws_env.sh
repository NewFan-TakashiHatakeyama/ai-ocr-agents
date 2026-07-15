#!/usr/bin/env bash
# AI-OCR の AWS 環境を起動/停止する（月に数回しか使わない前提の運用スクリプト）。
#
# 立てっぱなしは $606/月。ElastiCache / NAT / ALB は AWS 側に「停止」が無く削除しかない
# ため、止めたいなら destroy が基本になる。
#
#   up      terraform apply（15-20分。RDS 作成が支配的）      → 起動中 $0.766/h
#   down    スナップショットを取ってから destroy             → 残 $3.95/月（ECR＋snapshot）
#   pause   Fargate だけ 0 台（数十秒。DB/URL は保持）        → 残 $189/月 ※短期の中断用
#   resume  pause の解除
#   status  今どの状態か・何が課金されているか
#   cost    起動中の時間単価と、この環境の概算
#
#   vl-up   VL フォールバックを起動（GPU g4dn.xlarge。+$0.710/h）
#   vl-down VL を停止（GPU インスタンス 0 台。課金が止まる）
#   vl-test VL が実際に動くか確認（/health と実推論）
#
# 使い方: scripts/aws_env.sh up|down|pause|resume|status|cost|vl-up|vl-down|vl-test
set -euo pipefail

TF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../deploy/terraform" && pwd)"
TFVARS="${TFVARS:-env/production.tfvars}"
REGION="${AWS_REGION:-ap-northeast-1}"

# 起動中の時間単価（AWS Pricing API 実取得値, ap-northeast-1）
HOURLY_FARGATE="0.5293" # gateway x2 + orchestrator + export + structure(4vCPU) + ocr(2vCPU)
HOURLY_FIXED="0.2363"   # RDS + ElastiCache + NAT + ALB
HOURLY_TOTAL="0.7656"

tf() { (cd "$TF_DIR" && terraform "$@"); }

_prefix() { (cd "$TF_DIR" && terraform output -raw cluster_name 2>/dev/null || echo "ai-ocr-production"); }

cmd_cost() {
  cat <<EOF
起動中の実費（AWS Pricing API 実取得値, ap-northeast-1）
  Fargate 5サービス   \$${HOURLY_FARGATE}/h
  RDS+Redis+NAT+ALB   \$${HOURLY_FIXED}/h
  ------------------------------------
  合計                \$${HOURLY_TOTAL}/h  (約 114 円/時)

  8時間使うと  約 \$6.1   (約   919円)
  1日(24h)     約 \$18.4  (約 2,756円)

停止時に残るもの
  down (destroy) : ECR \$3.00 + スナップショット 約\$0.95 = 約 \$3.95/月
  pause          : 約 \$189/月（ElastiCache/NAT/ALB は停止できないため）

参考: 立てっぱなし = \$606.69/月（約91,000円）
      月4回x8h の down 運用 = 約 \$28/月（約4,300円）
EOF
}

cmd_status() {
  local cluster; cluster="$(_prefix)"
  echo "== terraform state =="
  if ! (cd "$TF_DIR" && terraform state list >/dev/null 2>&1) || [ -z "$(cd "$TF_DIR" && terraform state list 2>/dev/null)" ]; then
    echo "  リソースなし（down 状態）→ 課金は ECR とスナップショットのみ（約 \$3.95/月）"
    echo
    echo "== 残っているスナップショット =="
    aws rds describe-db-snapshots --region "$REGION" --snapshot-type manual \
      --query "DBSnapshots[?starts_with(DBSnapshotIdentifier,'ai-ocr')].[DBSnapshotIdentifier,SnapshotCreateTime,AllocatedStorage]" \
      --output table 2>/dev/null || true
    return
  fi
  echo "  $(cd "$TF_DIR" && terraform state list | wc -l) リソースが存在（課金中）"
  echo
  echo "== ECS サービスの稼働数 =="
  aws ecs list-services --cluster "$cluster" --region "$REGION" --query "serviceArns" --output text 2>/dev/null \
    | tr '\t' '\n' | while read -r arn; do
        [ -z "$arn" ] && continue
        aws ecs describe-services --cluster "$cluster" --services "$arn" --region "$REGION" \
          --query "services[0].[serviceName,desiredCount,runningCount]" --output text 2>/dev/null
      done || true
  echo
  echo "※ Fargate が 0 台でも RDS/ElastiCache/NAT/ALB は課金され続ける（約 \$189/月）。"
  echo "  完全に止めるには down（destroy）が必要。"
}

cmd_up() {
  echo "[up] terraform apply を実行します（15-20分。RDS 作成が支配的）"
  echo "     起動後は \$${HOURLY_TOTAL}/h（約114円/時）が掛かります。"
  tf apply -var-file="$TFVARS" -var 'services_enabled=true'
  echo
  echo "[up] 完了。ALB: $(cd "$TF_DIR" && terraform output -raw alb_dns_name 2>/dev/null || echo '-')"
  echo "     使い終わったら必ず: scripts/aws_env.sh down"
}

cmd_pause() {
  echo "[pause] Fargate を 0 台にします（DB とエンドポイントは保持）"
  echo "        ※ ElastiCache/NAT/ALB は停止できないため 約 \$189/月 は残ります。"
  echo "          月単位で使わないなら down を使ってください。"
  tf apply -var-file="$TFVARS" -var 'services_enabled=false'
}

cmd_resume() {
  tf apply -var-file="$TFVARS" -var 'services_enabled=true'
}

cmd_down() {
  local snap; snap="ai-ocr-production-$(date +%Y%m%d-%H%M%S)"
  local db_id; db_id="$(cd "$TF_DIR" && terraform output -raw db_endpoint 2>/dev/null | cut -d. -f1 || true)"

  if [ -n "$db_id" ]; then
    echo "[down] DB スナップショットを取得: $snap （destroy でデータが消えるため）"
    aws rds create-db-snapshot --region "$REGION" \
      --db-instance-identifier "$db_id" --db-snapshot-identifier "$snap" \
      --tags Key=Service,Value=ai-ocr Key=Name,Value="$snap" >/dev/null
    echo "[down] スナップショット完了を待機中..."
    aws rds wait db-snapshot-completed --region "$REGION" --db-snapshot-identifier "$snap"
    echo "[down] 完了: $snap"
  else
    echo "[down] RDS が見つかりません。スナップショットをスキップします。"
  fi

  echo "[down] terraform destroy を実行します"
  tf destroy -var-file="$TFVARS"
  echo
  echo "[down] 完了。残る課金は ECR（約 \$3/月）とスナップショット（約 \$1/月）だけです。"
  echo "       復元が必要な場合はこのスナップショットから手動で RDS を作成してください: $snap"
}

# ---------- VL フォールバック（GPU） ----------
# GPU が要るのは vlm-server だけ（vl-svc は CPU Fargate。実測で確認）。
# 課金は ASG の desired_capacity で切る。vl_enabled=false で 0 台になる。

cmd_vl_up() {
  echo "[vl-up] VL を起動します（GPU g4dn.xlarge が立ち上がり +\$0.710/h 課金されます）"
  echo "        vLLM のモデルロードに数分かかります。"
  tf apply -var-file="$TFVARS" -var 'vl_enabled=true' -auto-approve
  echo "[vl-up] GPU インスタンスと vlm-server の起動を待機中..."
  aws ecs wait services-stable --region "$REGION" --cluster "$(_prefix)" \
    --services ai-ocr-vlm-server ai-ocr-vl-svc 2>/dev/null || true
  cmd_vl_test
}

cmd_vl_down() {
  echo "[vl-down] VL を停止します（GPU インスタンスを 0 台にし課金を止めます）"
  tf apply -var-file="$TFVARS" -var 'vl_enabled=false' -auto-approve
  echo "[vl-down] 完了。GPU の課金は止まりました（VL 以外は起動したままです）。"
}

cmd_vl_test() {
  local cluster; cluster="$(_prefix)"
  echo "== VL の稼働状況 =="
  for s in ai-ocr-vlm-server ai-ocr-vl-svc; do
    aws ecs describe-services --region "$REGION" --cluster "$cluster" --services "$s" \
      --query "services[0].[serviceName,desiredCount,runningCount]" --output text 2>/dev/null \
      || echo "  $s: 未作成（vl_enabled=false）"
  done
  echo
  echo "== GPU インスタンス =="
  aws autoscaling describe-auto-scaling-groups --region "$REGION" \
    --auto-scaling-group-names ai-ocr-vlm \
    --query "AutoScalingGroups[0].[DesiredCapacity,length(Instances)]" --output text 2>/dev/null \
    || echo "  ASG なし"
  echo
  echo "== 実推論の確認 =="
  echo "  orchestrator-worker から VL へ実際に投げるには:"
  echo "    scripts/vl_smoke.sh <画像パス>"
}

case "${1:-}" in
  up)      cmd_up ;;
  down)    cmd_down ;;
  pause)   cmd_pause ;;
  resume)  cmd_resume ;;
  status)  cmd_status ;;
  cost)    cmd_cost ;;
  vl-up)   cmd_vl_up ;;
  vl-down) cmd_vl_down ;;
  vl-test) cmd_vl_test ;;
  *) sed -n '2,22p' "${BASH_SOURCE[0]}"; exit 1 ;;
esac
