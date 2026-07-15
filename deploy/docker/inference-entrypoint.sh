#!/bin/sh
# 推論サービング起動 entrypoint。
#
# 起動前に必ず設定を検証する（inference/README.md）。実在性チェックは paddlex が
# 入った環境でしか働かないため、CI ではなく**このコンテナで実行することが要件**。
# 検証で落ちれば起動しない（存在しないモデル名などを本番に出さない）。
set -e

: "${PIPELINE_CONFIG:?PIPELINE_CONFIG is required (e.g. /opt/inference/structure/pipeline_config.yaml)}"
LANG_CODE="${TENANT_LANG:-ja}"
# 既定 paddle（＝paddle_static + oneDNN）。paddle 3.2.2 では oneDNN が正常に効き、
# onnxruntime より約2倍速い（8vCPU 実測: 11.6s vs 23.7s）。精度は同一。
# 印章ありオプションは onnx パッケージが無く paddle でしか動かないため、既定を揃えておく。
# ※ paddle 3.3.1 では oneDNN/PIR バグで 500 になるため、この既定は使えない
#   （Dockerfile で 3.2.2 に固定している理由。バージョンを上げる際は必ず再検証すること）。
# onnxruntime に切り替えたい場合は INFERENCE_ENGINE=onnxruntime（印章ありでは使用不可）。
ENGINE="${INFERENCE_ENGINE:-paddle}"
PORT="${SERVE_PORT:-8080}"

# VL は genai サーバの URL がデプロイ先で決まる（compose は paddleocr-vlm-server、
# ECS は Service Connect の alias）。config に固定で書くと片方で解決できないため、
# VLM_SERVER_URL が来たら server_url を差し替える。read-only マウントでも書けるよう
# 実体を /tmp にコピーしてから使う。
if [ -n "${VLM_SERVER_URL:-}" ]; then
  RESOLVED="/tmp/pipeline_config.resolved.yaml"
  sed "s#^\( *server_url: \).*#\1${VLM_SERVER_URL}#" "${PIPELINE_CONFIG}" > "${RESOLVED}"
  echo "[entrypoint] server_url を ${VLM_SERVER_URL} に差し替えました"
  PIPELINE_CONFIG="${RESOLVED}"
fi

echo "[entrypoint] validating ${PIPELINE_CONFIG} (lang=${LANG_CODE})"
python /opt/inference/scripts/validate_config.py "${PIPELINE_CONFIG}" --lang "${LANG_CODE}"

echo "[entrypoint] starting paddlex --serve (engine=${ENGINE}, port=${PORT})"
exec paddlex --serve \
  --pipeline "${PIPELINE_CONFIG}" \
  --host 0.0.0.0 \
  --port "${PORT}" \
  --engine "${ENGINE}"
