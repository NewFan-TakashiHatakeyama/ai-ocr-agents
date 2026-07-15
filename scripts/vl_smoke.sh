#!/usr/bin/env bash
# VL フォールバックの実動作確認（§5.4）。
#
# VPC 内でしか vl-svc に到達できないため、orchestrator-worker のタスク定義を使って
# クラスタ内から実際に画像を投げる。/health だけでは「VL が読めた」ことにならないので、
# 実際に layout-parsing を叩いて中身（テキストが取れたか）まで見る。
#
# 使い方: scripts/vl_smoke.sh [S3のキー]
#   例: scripts/vl_smoke.sh ten_1/doc_xxx/pages/1.png
set -euo pipefail

TF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../deploy/terraform" && pwd)"
REGION="${AWS_REGION:-ap-northeast-1}"
CLUSTER="ai-ocr-production"
KEY="${1:-}"

if [ -z "$KEY" ]; then
  echo "使い方: scripts/vl_smoke.sh <S3のキー（例: ten_1/doc_xxx/pages/1.png）>" >&2
  exit 1
fi

BUCKET="$(cd "$TF_DIR" && terraform output -raw s3_bucket 2>/dev/null || echo "")"
SG="$(cd "$TF_DIR" && terraform output -raw service_security_group_id)"
SUBNETS="$(aws ec2 describe-subnets --region "$REGION" \
  --filters "Name=tag:Name,Values=ai-ocr-private-*" \
  --query "Subnets[].SubnetId" --output text | tr '\t' ',')"
TD="$(aws ecs describe-task-definition --region "$REGION" \
  --task-definition ai-ocr-orchestrator-worker \
  --query "taskDefinition.taskDefinitionArn" --output text)"

# vl-svc は Service Connect の alias でしか引けない。run-task には SC が付かないため、
# VL サービスの実 IP を Cloud Map から取って直接叩く。
VL_IP="$(aws servicediscovery discover-instances --region "$REGION" \
  --namespace-name "ai-ocr-production" --service-name "vl" \
  --query "Instances[0].Attributes.AWS_INSTANCE_IPV4" --output text 2>/dev/null || echo "")"

if [ -z "$VL_IP" ] || [ "$VL_IP" = "None" ]; then
  echo "vl-svc が見つかりません。先に scripts/aws_env.sh vl-up を実行してください。" >&2
  exit 1
fi
echo "[vl-smoke] vl-svc = ${VL_IP}:8080 / 画像 = s3://${BUCKET}/${KEY}"

SCRIPT=$(cat <<PY
import base64, json, os, sys, time
import boto3, httpx

s3 = boto3.client("s3")
img = s3.get_object(Bucket="${BUCKET}", Key="${KEY}")["Body"].read()
print("画像取得:", len(img), "bytes")

url = "http://${VL_IP}:8080/layout-parsing"
t0 = time.time()
r = httpx.post(url, json={
    "file": base64.b64encode(img).decode(),
    "fileType": 1,
    "visualize": False,
}, timeout=600.0)
dt = time.time() - t0
print("HTTP", r.status_code, "| %.1f 秒" % dt)
if r.status_code != 200:
    print("BODY:", r.text[:400]); sys.exit(1)

res = r.json()["result"]["layoutParsingResults"][0]
md = (res.get("markdown") or {}).get("text", "")
print("markdown 文字数:", len(md))
print("--- 先頭 300 文字 ---")
print(md[:300])
print("VL 実推論 OK" if md.strip() else "VL は 200 だが本文が空（要調査）")
PY
)

# overrides は JSON。python でエスケープする（Windows の素の `python` は
# Microsoft Store のスタブで無言終了するため PY_BIN で明示する）。
PY_BIN="${PY_BIN:-uv run python}"
OVERRIDES=$($PY_BIN -c "import json,sys; print(json.dumps({'containerOverrides':[{'name':'orchestrator-worker','command':['python','-c',sys.argv[1]]}]}))" "$SCRIPT")

ARN=$(aws ecs run-task --region "$REGION" --cluster "$CLUSTER" --task-definition "$TD" \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[${SUBNETS}],securityGroups=[${SG}],assignPublicIp=DISABLED}" \
  --overrides "$OVERRIDES" \
  --query "tasks[0].taskArn" --output text)

TID="${ARN##*/}"
echo "[vl-smoke] タスク実行中: $TID"
aws ecs wait tasks-stopped --region "$REGION" --cluster "$CLUSTER" --tasks "$ARN"
sleep 8
aws logs get-log-events --region "$REGION" \
  --log-group-name "/ecs/ai-ocr-orchestrator-worker" \
  --log-stream-name "ecs/orchestrator-worker/${TID}" \
  --query "events[].message" --output text
