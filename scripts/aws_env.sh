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
#   seed-schemas  field_schemas を投入（golden/data/schemas.json）
#   probe-rls  実 RDS で RLS が効いているかを検査（§7.3）
#   golden  ゴールデンセット回帰（§14.2）を実系に流して測り、リリースゲートを判定
#   redrive S3 トリガーの DLQ を主キューへ戻す（down→up 後にワークフロー定義を
#           復元してから実行。§16 P4）
#   sink-demo P6 デモ用の書込み先（erp_demo スキーマ + erp_sink ロール）を用意
#           （SINK_PASSWORD 必須。§16 P6）
#
# 使い方: scripts/aws_env.sh up|down|pause|resume|status|cost|vl-up|vl-down|vl-test|token|golden|redrive
set -euo pipefail

TF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../deploy/terraform" && pwd)"
TFVARS="${TFVARS:-env/production.tfvars}"
REGION="${AWS_REGION:-ap-northeast-1}"
ENV_NAME="${ENV_NAME:-production}"
ACCOUNT_ID="${ACCOUNT_ID:-654654601240}"
DEV_TENANT="${DEV_TENANT:-ten_1}"
# Windows の素の `python` は Microsoft Store のスタブで無言終了するため明示する
PY_BIN="${PY_BIN:-uv run python}"
# aws CLI に /aws/... のようなパスを渡すと MSYS が Windows パスへ変換して壊すため無効化する。
export MSYS_NO_PATHCONV=1
# Windows の Python は stdout が cp932 になり、$(python -c "print(...)") で捕捉した
# 日本語が cp932 バイトのまま次のプロセスへ渡って DB に化けて入る
# （seed-schemas のラベル文字化けとして実際に発生）。全 Python 呼び出しを UTF-8 に固定する。
export PYTHONUTF8=1

# 起動中の時間単価（AWS Pricing API 実取得値, ap-northeast-1）
HOURLY_FARGATE="0.5293" # gateway x2 + orchestrator + export + structure(4vCPU) + ocr(2vCPU)
HOURLY_FIXED="0.2363"   # RDS + ElastiCache + NAT + ALB
HOURLY_TOTAL="0.7656"

# 依存コマンドの事前チェック。無いまま進むと set -e で「127 で無言に落ちる」ため、
# 何が無いか・どう入れるかを先に言う。WSL の bash で実行した場合もここで検出される
# （aws/uv は Windows 側にしか無いことが多い）。PowerShell からは scripts/aws_env.ps1 を使う。
_require() {
  local missing=()
  local c
  for c in "$@"; do
    command -v "$c" >/dev/null 2>&1 || missing+=("$c")
  done
  if [ ${#missing[@]} -gt 0 ]; then
    echo "[aws_env] 必要なコマンドが見つかりません: ${missing[*]}" >&2
    echo "  - Git Bash（Windows）から実行してください（WSL では PATH が異なります）" >&2
    echo "  - PowerShell の場合: powershell -File scripts/aws_env.ps1 <command>" >&2
    echo "  - aws: AWS CLI v2 / uv: https://docs.astral.sh/uv/ / terraform, docker も同様" >&2
    exit 1
  fi
}

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
  # S3 トリガーの滞留（§16 P4）。down 中に inbox へ置かれた分がここに溜まり、
  # up すると orchestrator-worker が drain する。ecr スタック未適用ならキューが
  # 無いだけなので黙って飛ばす。
  local qurl
  qurl="$(aws sqs get-queue-url --region "$REGION" --queue-name ai-ocr-workflow-trigger \
    --query QueueUrl --output text 2>/dev/null || true)"
  if [ -n "$qurl" ] && [ "$qurl" != "None" ]; then
    echo "== S3 トリガー（SQS 滞留） =="
    aws sqs get-queue-attributes --region "$REGION" --queue-url "$qurl" \
      --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible \
      --query "Attributes.[ApproximateNumberOfMessages,ApproximateNumberOfMessagesNotVisible]" \
      --output text 2>/dev/null | awk '{print "  待機中: "$1"  処理中: "$2}' || true
    local dlq
    dlq="$(aws sqs get-queue-url --region "$REGION" --queue-name ai-ocr-workflow-trigger-dlq \
      --query QueueUrl --output text 2>/dev/null || true)"
    if [ -n "$dlq" ] && [ "$dlq" != "None" ]; then
      local n
      n="$(aws sqs get-queue-attributes --region "$REGION" --queue-url "$dlq" \
        --attribute-names ApproximateNumberOfMessages \
        --query "Attributes.ApproximateNumberOfMessages" --output text 2>/dev/null || echo 0)"
      echo "  DLQ: ${n} 件$([ "${n:-0}" != "0" ] && echo '  ★ワークフロー定義を復元してから redrive で戻せます')"
    fi
    echo
  fi
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

  # web は NEXT_PUBLIC_API_BASE をビルド時に焼き込む（実行時 env では変えられない、
  # web.Dockerfile 参照）。ALB は down/up の再作成のたびに DNS 名が変わるため、
  # タグの有無だけで判定すると destroy 済みの旧 ALB を指す古いイメージを再利用してしまい、
  # up 直後のフロントエンドが全 API 呼び出しで Failed to fetch になる
  # （実発生・down→up 検証で検出。焼き込み値は runtime イメージの config に残らず
  # 事後検出できないため、web だけは常に再ビルドする）。
  local alb; alb="$(cd "$TF_DIR" && terraform output -raw alb_dns_name 2>/dev/null || echo "")"

  # app 系（コードが頻繁に変わる）は、タグが在っても「リポジトリの最新コミットより
  # 古い」なら作り直す。image_tag 据え置き + タグ存在スキップの組合せで 7/21 の
  # 古いイメージを掴み、新機能欠落と migration 未適用が実際に起きた（レビュー確定）。
  # inference/vl-* は巨大ビルドかつ変更が稀なため従来どおりタグ有無のみ
  # （変更した時は image_tag を上げるか、タグを手で消して push し直す）。
  local app_imgs=" gateway orchestrator-worker export-worker migrate "
  # git -C に MSYS の /c/... パスを渡すと MSYS_NO_PATHCONV=1 のため解決できない
  # （実測: fatal: cannot change to）。cd してから実行する
  local repo_epoch; repo_epoch="$(cd "$repo_root" && git log -1 --format=%ct)"
  local missing=()
  for i in gateway orchestrator-worker export-worker migrate inference web vl-pipeline vlm-server; do
    if [ "$i" = "web" ]; then
      [ -z "$alb" ] && { echo "  [!] ALB 未確定のため web をビルドできません（apply 後に実行してください）" >&2; return 1; }
      echo "  ai-ocr-${i}:${tag} … ALB 依存のため常に再ビルドします"
      missing+=("$i")
    elif _ecr_has_tag "$i" "$tag"; then
      if [[ "$app_imgs" == *" $i "* ]]; then
        pushed="$(aws ecr describe-images --region "$REGION" --repository-name "ai-ocr-$i" --image-ids "imageTag=$tag" --query "imageDetails[0].imagePushedAt" --output text 2>/dev/null)"
        pushed_epoch="$(${PY_BIN} -c "import datetime,sys;print(int(datetime.datetime.fromisoformat(sys.argv[1]).timestamp()))" "$pushed" 2>/dev/null || echo 0)"
        if [ "${pushed_epoch:-0}" -lt "$repo_epoch" ]; then
          echo "  ai-ocr-${i}:${tag} … あり（最新コミットより古いため再ビルドします）"
          missing+=("$i")
        else
          echo "  ai-ocr-${i}:${tag} … あり（最新）"
        fi
      else
        echo "  ai-ocr-${i}:${tag} … あり"
      fi
    else
      echo "  ai-ocr-${i}:${tag} … 無し（ビルドします）"
      missing+=("$i")
    fi
  done

  aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$reg" >/dev/null

  # ECR は imageTagMutability=IMMUTABLE のため同タグへの push が拒否される。
  # 再ビルド対象（web の常時 + app 系の鮮度落ち）ごとに旧タグを削除してから push する。
  local i
  for i in "${missing[@]}"; do
    aws ecr batch-delete-image --region "$REGION" --repository-name "ai-ocr-${i}" \
      --image-ids imageTag="$tag" >/dev/null 2>&1 || true
  done

  for i in "${missing[@]}"; do
    # -f には相対パスを渡す。MSYS 環境の bash から /c/... 形式の絶対パスを渡すと
    # docker が解釈できず「resolve: CreateFile \c: …」で落ちる（実測）。
    local args=(-f "deploy/docker/${i}.Dockerfile" -t "${reg}/ai-ocr-${i}:${tag}")
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

  # push しただけでは稼働中タスクは旧イメージのまま残る（実測）。push した app 系
  # サービスへ force-new-deployment を打って確実に切り替える（クラスタ未作成時は無視）
  local svc
  for i in "${missing[@]}"; do
    case "$i" in
      gateway|web|orchestrator-worker|export-worker) svc="ai-ocr-${i}" ;;
      *) continue ;;
    esac
    aws ecs update-service --region "$REGION" --cluster "$(_prefix)" \
      --service "$svc" --force-new-deployment >/dev/null 2>&1 \
      && echo "  redeploy ${svc}" || true
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

  # §7.3 RLS: アプリは所有者でないロールで繋ぐ。terraform は接続情報（Secrets Manager の
  # app-database-url）しか作れないので、ロールの実体と GRANT はここで作る。
  # apply でパスワードが変わっても追随するよう、up のたびに流す。
  echo "[migrate] アプリ用ロール（RLS 適用側）を用意します"
  _run_in_migrate scripts/ensure_app_role.py || { echo "[migrate] ロール作成に失敗" >&2; return 1; }

  # langgraph のチェックポイント表（§4.4）。DDL なので所有者で作る。ワーカーは
  # 所有者でないロールになり CREATE 権限を持たないため、ここで作らないと全ジョブが落ちる。
  echo "[migrate] チェックポイント表を用意します"
  _run_in_migrate scripts/setup_checkpointer.py \
    || { echo "[migrate] チェックポイント表の作成に失敗" >&2; return 1; }

  # S3 イベント駆動トリガー用の s3 接続（§16 P4）。バケット名はアカウント ID を
  # 含み terraform が決めるため、up のたびにここで seed する（destroy で DB ごと
  # 消えても復元される）。
  echo "[migrate] inbox の s3 接続を投入します"
  _run_in_migrate scripts/seed_s3_connection.py "" \
    "TENANT_ID=${DEV_TENANT}" "INBOX_BUCKET=ai-ocr-inbox-${ACCOUNT_ID}" \
    || { echo "[migrate] s3 接続の投入に失敗" >&2; return 1; }
}

_run_in_migrate() {
  # 手元の python スクリプトを migrate タスクのコンテナ内で実行する。
  # RDS はプライベート VPC にいて手元からは繋がらないため、DB を触る作業は全部ここを通す。
  # スクリプトと引数は gzip+base64 で渡す（ECS の overrides は 8KB 上限で、生の JSON を
  # シェルに埋めると引用符で壊れる）。
  #   _run_in_migrate <script_path> [argv1] [env NAME=VALUE ...]
  local script="$1"; shift
  local arg="${1:-}"; [ $# -gt 0 ] && shift
  local cluster; cluster="$(_prefix)"
  local td sg subnets arn overrides code tid
  td="$(cd "$TF_DIR" && terraform output -raw migrate_task_definition_arn)"
  sg="$(cd "$TF_DIR" && terraform output -raw service_security_group_id)"
  subnets="$(aws ec2 describe-subnets --region "$REGION" \
    --filters "Name=tag:Name,Values=ai-ocr-private-*" \
    --query "Subnets[].SubnetId" --output text | tr '\t' ',')"

  overrides=$(${PY_BIN} -c "
import base64, gzip, json, pathlib, sys
script, arg = sys.argv[1], sys.argv[2]
envs = [dict(zip(('name', 'value'), a.split('=', 1))) for a in sys.argv[3:]]
enc = lambda b: base64.b64encode(gzip.compress(b)).decode('ascii')
src = enc(pathlib.Path(script).read_bytes())
name = pathlib.Path(script).name
# argv も JSON→gzip→base64 で渡す。スキーマ JSON は日本語とクォートを含むため、
# シェルやコンテナの command 配列に生で載せると壊れる。
argv = enc(json.dumps([name] + ([arg] if arg else []), ensure_ascii=False).encode('utf-8'))
code = (
    'import base64,gzip,json,sys;'
    f\"sys.argv=json.loads(gzip.decompress(base64.b64decode('{argv}')).decode('utf-8'));\"
    f\"exec(compile(gzip.decompress(base64.b64decode('{src}')).decode('utf-8'),'{name}','exec'),{{'__name__':'__main__'}})\"
)
print(json.dumps({'containerOverrides': [
    {'name': 'migrate', 'command': ['python', '-c', code], 'environment': envs}
]}))
" "$script" "$arg" "$@")

  arn=$(aws ecs run-task --region "$REGION" --cluster "$cluster" --task-definition "$td" \
    --launch-type FARGATE \
    --network-configuration "awsvpcConfiguration={subnets=[${subnets}],securityGroups=[${sg}],assignPublicIp=DISABLED}" \
    --overrides "$overrides" --query "tasks[0].taskArn" --output text)
  aws ecs wait tasks-stopped --region "$REGION" --cluster "$cluster" --tasks "$arn"
  code=$(aws ecs describe-tasks --region "$REGION" --cluster "$cluster" --tasks "$arn" \
    --query "tasks[0].containers[0].exitCode" --output text)
  tid="${arn##*/}"
  aws logs get-log-events --region "$REGION" --log-group-name "/ecs/ai-ocr-migrate" \
    --log-stream-name "ecs/migrate/${tid}" --query "events[].message" --output text || true
  return "$code"
}

cmd_seed_schemas() {
  # golden/data/schemas.json の field_schemas を投入する（§7）。
  local data
  data=$(${PY_BIN} -c "
import json, pathlib
doc = json.loads(pathlib.Path('golden/data/schemas.json').read_text(encoding='utf-8'))
# _comment は投入に不要。8KB 制限に効くので落とす。
print(json.dumps({'schemas': doc['schemas']}, ensure_ascii=False, separators=(',', ':')))
")
  echo "[seed] field_schemas を投入します（tenant=${DEV_TENANT}）"
  _run_in_migrate scripts/seed_schemas.py "$data" "TENANT_ID=${DEV_TENANT}" \
    || { echo "[seed] 失敗" >&2; return 1; }
  echo "[seed] 完了"
}

cmd_probe_rls() {
  # RLS が実 RDS で効いているかを測る（§7.3）。ローカル compose の newfan は
  # superuser で無条件にバイパスするため、ローカルでは検証にならない。
  echo "[rls] 実 RDS で RLS を検査します"
  _run_in_migrate scripts/probe_rls.py \
    || { echo "[rls] ★RLS が効いていません（上のログを参照）" >&2; return 1; }
  echo "[rls] RLS は有効です"
}

cmd_golden() {
  # ゴールデンセット回帰（§14.2 / DD-03）を**実際に動いている系**に流して測る。
  # 本番昇格の条件なので Fake は使わない。gold は既定で dev サンプル 2 件。
  local gold="${GOLD:-golden/data/dev.jsonl}"
  local out="${GOLDEN_OUT:-out/golden}"
  local base token
  base="$(cd "$TF_DIR" && terraform output -raw alb_dns_name 2>/dev/null)" || {
    echo "[golden] ALB が見つかりません（up 済みですか）" >&2; return 1; }
  token="$(cmd_token)"
  mkdir -p "$out"

  echo "[golden] ${gold} を http://${base}/v1 に流します"
  uv run --extra collect --package newfan-golden python -m newfan_golden.collect \
    --gold "$gold" --api "http://${base}/v1" --token "$token" --out "${out}/pred.jsonl"

  local baseline=""
  [ -f golden/baselines/production.json ] && baseline="--baseline golden/baselines/production.json"
  ${PY_BIN} -m newfan_golden.cli --gold "$gold" --pred "${out}/pred.jsonl" \
    --out "${out}/metrics.json" $baseline
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
  #
  # 注意: ECR は長命スタック（down で消えない）。apply の時点で web:${tag} が
  # 前回 up の（今の ALB とは別の DNS を焼き込んだ）古いイメージのまま存在するため、
  # ECS はここで既に古いイメージの pull・タスク起動を始めている。cmd_push が
  # 新イメージを push しても、起動済みタスクは自動で置き換わらない
  # （実発生: up 直後の web が Failed to fetch で全滅した）。
  # push 後に web サービスへ force-new-deployment を打って確実に新イメージへ切り替える。
  cmd_push || return 1
  aws ecs update-service --region "$REGION" --cluster "$(_prefix)" \
    --service ai-ocr-web --force-new-deployment >/dev/null
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

cmd_sink_demo() {
  # P6 デモ/E2E: 「顧客基幹 DB」役の erp_demo スキーマ + erp_sink ロールを用意する。
  # パスワードは env SINK_PASSWORD（Secrets Manager ai-ocr/<env>/conn/<tenant>/... に
  # 登録した値と同じものを渡す）。冪等。
  [ -z "${SINK_PASSWORD:-}" ] && { echo "[sink-demo] SINK_PASSWORD を設定してください" >&2; return 1; }
  echo "[sink-demo] erp_demo スキーマと erp_sink ロールを用意します"
  _run_in_migrate scripts/setup_sink_demo.py "" "SINK_PASSWORD=${SINK_PASSWORD}"     || { echo "[sink-demo] 失敗" >&2; return 1; }
}

cmd_redrive() {
  # DLQ に退避した S3 トリガーイベントを主キューへ戻す（§16 P4）。
  # 想定シナリオ: down（DB destroy）→ up 直後は active なワークフローが無く、
  # consumer は滞留イベントを削除せず保留 → 5 回で DLQ に落ちる。定義を復元
  #（API/SCR-07 で再作成 or スナップショット復元）した後にこれを実行すると再処理される。
  local dlq main
  dlq="$(aws sqs get-queue-url --region "$REGION" --queue-name ai-ocr-workflow-trigger-dlq     --query QueueUrl --output text 2>/dev/null)" || { echo "[redrive] DLQ がありません" >&2; return 1; }
  main="$(aws sqs get-queue-url --region "$REGION" --queue-name ai-ocr-workflow-trigger     --query QueueUrl --output text 2>/dev/null)" || { echo "[redrive] 主キューがありません" >&2; return 1; }
  local n
  n="$(aws sqs get-queue-attributes --region "$REGION" --queue-url "$dlq"     --attribute-names ApproximateNumberOfMessages     --query "Attributes.ApproximateNumberOfMessages" --output text)"
  if [ "${n:-0}" = "0" ]; then echo "[redrive] DLQ は空です"; return 0; fi
  local dlq_arn
  dlq_arn="$(aws sqs get-queue-attributes --region "$REGION" --queue-url "$dlq"     --attribute-names QueueArn --query "Attributes.QueueArn" --output text)"
  echo "[redrive] DLQ の ${n} 件を主キューへ戻します"
  aws sqs start-message-move-task --region "$REGION" --source-arn "$dlq_arn" >/dev/null     || { echo "[redrive] 失敗" >&2; return 1; }
  echo "[redrive] 開始しました。数十秒で反映されます（status で確認）。"
}

# コマンド別の依存（全部に docker を要求すると status すら打てなくなるため分ける）
case "${1:-}" in
  up|push) _require aws uv terraform docker ;;
  down|pause|resume|vl-up|vl-down) _require aws uv terraform ;;
  migrate|seed-schemas|probe-rls|probe-role|golden|sink-demo) _require aws uv terraform ;;
  token|status|cost|vl-test|redrive) _require aws uv ;;
esac

case "${1:-}" in
  up)      cmd_up ;;
  push)    cmd_push ;;
  migrate) cmd_migrate ;;
  seed-schemas) cmd_seed_schemas ;;
  probe-rls) cmd_probe_rls ;;
  probe-role) _run_in_migrate scripts/probe_role.py ;;
  golden)  cmd_golden ;;
  token)   cmd_token ;;
  down)    cmd_down ;;
  pause)   cmd_pause ;;
  resume)  cmd_resume ;;
  status)  cmd_status ;;
  cost)    cmd_cost ;;
  vl-up)   cmd_vl_up ;;
  vl-down) cmd_vl_down ;;
  vl-test) cmd_vl_test ;;
  redrive) cmd_redrive ;;
  sink-demo) cmd_sink_demo ;;
  *) sed -n '2,22p' "${BASH_SOURCE[0]}"; exit 1 ;;
esac
