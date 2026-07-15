#!/usr/bin/env bash
# AI-OCR の AWS 環境を起動/停止する（月に数回しか使わない前提の運用スクリプト）。
#
# 立てっぱなしは $606/月。ElastiCache / NAT / ALB は AWS 側に「停止」が無く削除しかない
# ため、止めたいなら destroy が基本になる。
#
#   up      これ 1 つで起動（apply → 不足イメージを push → migrate → テナント投入）
#           15-20 分。RDS 作成が支配的                          → 起動中 $0.766/h
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
#   token   検証用 JWT を発行（web の localStorage["nf_token"] に入れる）
#   push    ECR に不足しているイメージだけ push（up が内部で呼ぶ）
#   migrate DB マイグレーション＋dev テナント投入（up が内部で呼ぶ）
#
# 使い方: scripts/aws_env.sh up|down|pause|resume|status|cost|vl-up|vl-down|vl-test|token
set -euo pipefail

TF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../deploy/terraform" && pwd)"
TFVARS="${TFVARS:-env/production.tfvars}"
REGION="${AWS_REGION:-ap-northeast-1}"
ENV_NAME="${ENV_NAME:-production}"
ACCOUNT_ID="${ACCOUNT_ID:-654654601240}"
DEV_TENANT="${DEV_TENANT:-ten_1}"
# Windows の素の `python` は Microsoft Store のスタブで無言終了するため明示する
PY_BIN="${PY_BIN:-uv run python}"
# aws CLI に /aws/... のようなパスを渡すと MSYS が Windows パスへ変換して壊す
export MSYS_NO_PATHCONV=1

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

_image_tag() {
  grep -E '^\s*image_tag' "$TF_DIR/$TFVARS" | head -1 | sed -E 's/.*"([^"]+)".*/\1/'
}

_ecr_has_tag() {
  aws ecr describe-images --region "$REGION" --repository-name "ai-ocr-$1" \
    --image-ids "imageTag=$2" >/dev/null 2>&1
}

cmd_push() {
  # ECR に image_tag が無ければ **Dockerfile からビルドして** push する。
  # ローカルの既存タグを使い回さないのは、古い config を焼いたイメージを掴む事故を
  # 実際に起こしたため（VL の layout モデルを直したのにイメージが古く AWS で落ちた）。
  # ECR は down（destroy）でも残るので、通常 2 回目以降はスキップされる。
  local tag; tag="$(_image_tag)"
  local reg="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"
  local repo_root; repo_root="$(cd "$TF_DIR/../.." && pwd)"
  echo "[push] image_tag=${tag} を確認します"

  local missing=()
  for i in gateway orchestrator-worker export-worker migrate inference web vl-pipeline vlm-server; do
    if _ecr_has_tag "$i" "$tag"; then
      echo "  ai-ocr-${i}:${tag} … あり"
    else
      echo "  ai-ocr-${i}:${tag} … 無し（ビルドします）"
      missing+=("$i")
    fi
  done
  [ ${#missing[@]} -eq 0 ] && { echo "[push] 全イメージが ECR にあります。スキップします。"; return 0; }

  aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$reg" >/dev/null

  # web は NEXT_PUBLIC_API_BASE をビルド時に焼き込む（実行時 env では変えられない）。
  # ALB の DNS は apply 後に確定するのでここで取る。
  local alb; alb="$(cd "$TF_DIR" && terraform output -raw alb_dns_name 2>/dev/null || echo "")"

  local i
  for i in "${missing[@]}"; do
    local args=(-f "${repo_root}/deploy/docker/${i}.Dockerfile" -t "${reg}/ai-ocr-${i}:${tag}")
    case "$i" in
      web)
        [ -z "$alb" ] && { echo "  [!] ALB 未確定のため web をビルドできません（apply 後に実行してください）" >&2; return 1; }
        args+=(--build-arg "NEXT_PUBLIC_API_BASE=http://${alb}/v1")
        ;;
      vl-pipeline)
        # 推論イメージを土台にする（VL の config もそこに入っている）
        args+=(--build-arg "BASE_IMAGE=${reg}/ai-ocr-inference:${tag}")
        ;;
    esac
    echo "  building ai-ocr-${i}:${tag} …"
    (cd "$repo_root" && docker build -q "${args[@]}" . >/dev/null) || {
      echo "  [!] ${i} のビルドに失敗しました" >&2; return 1; }
    docker push -q "${reg}/ai-ocr-${i}:${tag}" >/dev/null && echo "  pushed ai-ocr-${i}:${tag}"
  done
}

cmd_migrate() {
  # DB マイグレーション（run-task）＋ dev テナント投入。up の一部として毎回流す
  # （destroy で DB ごと消えるため、apply の直後は必ず空になる）。
  local cluster; cluster="$(_prefix)"
  local td sg subnets arn tid
  td="$(cd "$TF_DIR" && terraform output -raw migrate_task_definition_arn)"
  sg="$(cd "$TF_DIR" && terraform output -raw service_security_group_id)"
  subnets="$(aws ec2 describe-subnets --region "$REGION" \
    --filters "Name=tag:Name,Values=ai-ocr-private-*" \
    --query "Subnets[].SubnetId" --output text | tr '\t' ',')"

  echo "[migrate] alembic upgrade head を実行します"
  arn=$(aws ecs run-task --region "$REGION" --cluster "$cluster" --task-definition "$td" \
    --launch-type FARGATE \
    --network-configuration "awsvpcConfiguration={subnets=[${subnets}],securityGroups=[${sg}],assignPublicIp=DISABLED}" \
    --query "tasks[0].taskArn" --output text)
  aws ecs wait tasks-stopped --region "$REGION" --cluster "$cluster" --tasks "$arn"
  local code
  code=$(aws ecs describe-tasks --region "$REGION" --cluster "$cluster" --tasks "$arn" \
    --query "tasks[0].containers[0].exitCode" --output text)
  if [ "$code" != "0" ]; then
    tid="${arn##*/}"
    echo "[migrate] 失敗（exit=$code）。ログ:" >&2
    aws logs get-log-events --region "$REGION" --log-group-name "/ecs/ai-ocr-migrate" \
      --log-stream-name "ecs/migrate/${tid}" --query "events[-15:].message" --output text >&2 || true
    return 1
  fi
  echo "[migrate] 完了"

  echo "[migrate] dev テナント（${DEV_TENANT}）を投入します"
  arn=$(aws ecs run-task --region "$REGION" --cluster "$cluster" --task-definition "$td" \
    --launch-type FARGATE \
    --network-configuration "awsvpcConfiguration={subnets=[${subnets}],securityGroups=[${sg}],assignPublicIp=DISABLED}" \
    --overrides "$(cat <<JSON
{"containerOverrides":[{"name":"migrate","command":["python","-c","import os,psycopg; c=psycopg.connect(os.environ['DATABASE_URL'].replace('+psycopg','')); cur=c.cursor(); cur.execute(\"INSERT INTO tenants (id, name) VALUES ('${DEV_TENANT}','NewFan') ON CONFLICT (id) DO NOTHING\"); c.commit(); print('tenant ok')"]}]}
JSON
)" --query "tasks[0].taskArn" --output text)
  aws ecs wait tasks-stopped --region "$REGION" --cluster "$cluster" --tasks "$arn"
  echo "[migrate] テナント投入完了"
}

cmd_token() {
  # 検証用の JWT を発行して表示する。web は localStorage["nf_token"] を読む
  # （イメージに焼くと公開バンドルに JWT が入るため、ここで出して手で入れる）。
  local secret
  secret=$(aws secretsmanager get-secret-value --region "$REGION" \
    --secret-id "ai-ocr/${ENV_NAME}/jwt-secret" --query SecretString --output text 2>/dev/null) || {
    echo "[token] JWT シークレットが見つかりません（up 済みですか）" >&2; return 1; }
  ${PY_BIN} -c "
import jwt, sys
print(jwt.encode({'sub':'sato','tenant_id':'${DEV_TENANT}','role':'admin'}, sys.argv[1], algorithm='HS256'))
" "$secret"
}

cmd_up() {
  echo "[up] AI-OCR 環境を起動します。次を順に実行します:"
  echo "     0) ECR スタック（長命。既にあれば差分なし）"
  echo "     1) terraform apply（15-20分。RDS 作成が支配的）"
  echo "     2) ECR に不足イメージがあれば ビルドして push"
  echo "     3) DB マイグレーション＋dev テナント投入"
  echo "     起動後は \$${HOURLY_TOTAL}/h（約114円/時）が掛かります。"
  echo

  # ECR は本体と別スタック（down で消さないため）。本体は data 参照するので先に要る。
  echo "[up] ECR スタックを確認します"
  (cd "$TF_DIR/ecr" && terraform init -input=false >/dev/null && \
     terraform apply -input=false -auto-approve >/dev/null) \
    || { echo "[up] ECR スタックの apply に失敗しました" >&2; return 1; }

  tf apply -var-file="$TFVARS" -var 'services_enabled=true' -auto-approve

  # ECR は apply で作られる。イメージが無いとタスクが起動できないので先に push し、
  # そのあとサービスを安定させる。
  cmd_push || return 1
  cmd_migrate || return 1

  echo "[up] ECS サービスの安定を待機中..."
  aws ecs wait services-stable --region "$REGION" --cluster "$(_prefix)" \
    --services ai-ocr-gateway ai-ocr-web ai-ocr-orchestrator-worker \
               ai-ocr-structure-svc ai-ocr-ocr-svc 2>/dev/null || true

  local alb; alb="$(cd "$TF_DIR" && terraform output -raw alb_dns_name 2>/dev/null || echo '-')"
  cat <<EOF

[up] 完了しました。

  UI      : http://${alb}/dashboard
  API     : http://${alb}/v1
  ヘルス  : http://${alb}/healthz

  UI を開く前に、ブラウザの DevTools で認証トークンを入れてください:
    localStorage.setItem("nf_token", "\$(scripts/aws_env.sh token)")

  VL（GPU）を使う場合   : scripts/aws_env.sh vl-up
  使い終わったら必ず    : scripts/aws_env.sh down
EOF
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

  # スナップショットを取った直後で、意図は確定している。ここで対話を挟むと
  # 非対話実行（CI・バックグラウンド）で「approval: EOF」になり止まる。
  echo "[down] terraform destroy を実行します"
  tf destroy -var-file="$TFVARS" -auto-approve
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
  push)    cmd_push ;;
  migrate) cmd_migrate ;;
  token)   cmd_token ;;
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
