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
ARG PADDLE_VERSION=3.3.1
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

# モデルは初回起動時に ~/.paddlex へ取得される。Fargate ではタスク再作成のたびに再取得に
# なるため、本番はイメージへ焼くか EFS 等でキャッシュを共有すること（付録C-4）。
ENV PADDLE_PDX_CACHE_HOME=/root/.paddlex
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/inference-entrypoint.sh"]
