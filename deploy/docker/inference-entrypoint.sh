#!/bin/sh
# 推論サービング起動 entrypoint。
#
# 起動前に必ず設定を検証する（inference/README.md）。実在性チェックは paddlex が
# 入った環境でしか働かないため、CI ではなく**このコンテナで実行することが要件**。
# 検証で落ちれば起動しない（存在しないモデル名などを本番に出さない）。
set -e

: "${PIPELINE_CONFIG:?PIPELINE_CONFIG is required (e.g. /opt/inference/structure/pipeline_config.yaml)}"
LANG_CODE="${TENANT_LANG:-ja}"
# 既定 onnxruntime。**これは性能の選択ではなく必須**: paddle 3.3.1 では `--engine paddle`
# （= paddle_static）が oneDNN/PIR の未実装に当たり /layout-parsing が必ず 500 になる
#   NotImplementedError: ConvertPirAttribute2RuntimeAttribute not support ...
#     (new_executor/instruction/onednn/onednn_instruction.cc:116)
# Python API は enable_mkldnn=False で回避できるが、paddlex --serve に無効化手段が無い
# （CLI に該当フラグ無し。FLAGS_use_mkldnn=0 も効かないことを実測で確認）。
# paddle_dynamic は一部モデルが非対応。→ サービングで動くのは onnxruntime のみ。
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
