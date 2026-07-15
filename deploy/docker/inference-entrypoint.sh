#!/bin/sh
# 推論サービング起動 entrypoint。
#
# 起動前に必ず設定を検証する（inference/README.md）。実在性チェックは paddlex が
# 入った環境でしか働かないため、CI ではなく**このコンテナで実行することが要件**。
# 検証で落ちれば起動しない（存在しないモデル名などを本番に出さない）。
set -e

: "${PIPELINE_CONFIG:?PIPELINE_CONFIG is required (e.g. /opt/inference/structure/pipeline_config.yaml)}"
LANG_CODE="${TENANT_LANG:-ja}"
ENGINE="${INFERENCE_ENGINE:-onnxruntime}"
PORT="${SERVE_PORT:-8080}"

echo "[entrypoint] validating ${PIPELINE_CONFIG} (lang=${LANG_CODE})"
python /opt/inference/scripts/validate_config.py "${PIPELINE_CONFIG}" --lang "${LANG_CODE}"

echo "[entrypoint] starting paddlex --serve (engine=${ENGINE}, port=${PORT})"
exec paddlex --serve \
  --pipeline "${PIPELINE_CONFIG}" \
  --host 0.0.0.0 \
  --port "${PORT}" \
  --engine "${ENGINE}"
