# 推論サービング（structure-svc / ocr-svc 共用）。ADR-0003 Option A: Fargate CPU。
#
# 公式 paddleocr イメージを使わず自前で組む理由:
#   - `--engine onnxruntime` に必要な onnxruntime / paddle2onnx が公式イメージに入っている
#     保証が無く、無いと起動時に DependencyError になる（実測 2.5× 高速化を落とせない）。
#   - `paddlex --serve` には serving プラグインが別途必要（未導入だと DependencyError）。
#   - バージョンを固定して再現性を担保する（DD-03: 既定依存の禁止）。
#
# 使い分けは PIPELINE_CONFIG 環境変数で行う（1 イメージ = 2 サービス）:
#   structure-svc: PIPELINE_CONFIG=/opt/inference/structure/pipeline_config.yaml
#   ocr-svc:       PIPELINE_CONFIG=/opt/inference/ocr/pipeline_config.yaml
#
#   docker build -f deploy/docker/inference.Dockerfile -t newfan-inference .
#
# 注: OpenVINO は採用しない。ultra-infer の OpenVINO が PP-OCRv6_medium_rec を読めず落ちる
#     （inference/README.md の実測記録を参照）。よって hpi-cpu は導入しない。

FROM python:3.12-slim

# paddle の実行時共有ライブラリ（libgomp が無いと import paddle で落ちる）
RUN apt-get update && apt-get install -y --no-install-recommends \
      libgomp1 libgl1 libglib2.0-0 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 実測・検証した組成に固定する（付録C-1/C-2, DD-03）
#
# ★ paddlepaddle は **3.2.2 に固定**（最新の 3.3.1 を使わない）。実測で確認した理由:
#   3.3.1 は oneDNN/PIR が未実装のパスに当たり、`paddlex --serve` の /layout-parsing が必ず 500
#     NotImplementedError: ConvertPirAttribute2RuntimeAttribute not support
#       (new_executor/instruction/onednn/onednn_instruction.cc:116)
#   回避策が無い（CLI に oneDNN 無効化フラグ無し・FLAGS_use_mkldnn=0 も効かない）ため
#   3.3.1 では onnxruntime しか使えず、印章オプションも起動できなかった。
#   3.2.2 では oneDNN が正常動作し、**同一精度のまま約2倍速**（8vCPU: 11.6s vs onnx 23.7s）で、
#   印章ありオプションも動く（11.5s）。精度は spans=94/13行/conf 0.9678 と 3.3.1 と完全一致。
#   なお paddlex 3.7.2 の HPI 対応表も paddle30/31/311 までで 3.3 系は「未対応」扱い。
#   3.3 系で oneDNN が直ったら再評価すること。
ARG PADDLE_VERSION=3.2.2
ARG PADDLEOCR_VERSION=3.7.0
ARG PADDLEX_VERSION=3.7.2
RUN pip install --no-cache-dir \
      "paddlepaddle==${PADDLE_VERSION}" \
      "paddleocr==${PADDLEOCR_VERSION}" \
      "paddlex[ocr]==${PADDLEX_VERSION}" \
      onnxruntime paddle2onnx pyyaml

# paddlex --serve に必要なプラグイン
RUN paddlex --install serving

COPY inference/ /opt/inference/
COPY deploy/docker/inference-entrypoint.sh /usr/local/bin/inference-entrypoint.sh
RUN chmod +x /usr/local/bin/inference-entrypoint.sh

# モデルをイメージへ焼き込む（コールドスタート対策）。
# Fargate はタスク再作成のたびに新規コンテナになるため、起動時 DL だと毎回数百MB を取りに行き
# 起動が延びる（暫定で startPeriod=300 を置いていた）。ビルド時に取得して同梱する。
# 対象は「設定が参照するモデルのみ」= 通常構成 + 印章ありオプション + ocr。
RUN python /opt/inference/scripts/prefetch_models.py \
      /opt/inference/structure/pipeline_config.yaml \
      /opt/inference/structure/pipeline_config.seal.yaml \
      /opt/inference/ocr/pipeline_config.yaml
ENV PADDLE_PDX_CACHE_HOME=/root/.paddlex
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/inference-entrypoint.sh"]
