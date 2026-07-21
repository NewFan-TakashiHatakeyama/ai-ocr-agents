# vlm-server（PaddleOCR-VL の genai サーバ, §5.4）。**GPU 必須はここだけ**。
#
# vl-svc（パイプライン）は CPU で動き、VL 認識だけを本サーバへ委譲する
# （inference/vl/pipeline_config.yaml の genai_config.backend=vllm-server）。
#
# ベースは PaddleOCR 公式の vLLM サーバイメージ（18.3GB）。CUDA + vLLM 一式が入っている。
# 自前で vllm を積むより、公式が検証した組み合わせに乗る方が安全。
# 参照: PaddleOCR/deploy/paddleocr_vl_docker/accelerators/nvidia-gpu/vlm.Dockerfile
ARG BASE_IMAGE=ccr-2vdh3abv-pub.cnc.bj.baidubce.com/paddlepaddle/paddlex-genai-vllm-server:latest

FROM ${BASE_IMAGE}

# 我々の推論スタックとバージョンを揃える（DD-03: 版は明示固定）。
ARG PADDLEOCR_VERSION=3.7.0
ARG PADDLEX_VERSION=3.7.2
RUN python -m pip install --no-cache-dir \
      "paddleocr==${PADDLEOCR_VERSION}" "paddlex==${PADDLEX_VERSION}"

# モデルを焼き込む。起動時に取得させると GPU インスタンスの課金時間が伸び、
# 外部の可用性にも依存する（推論イメージと同じ方針）。
ARG MODEL_URL=https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/paddle3.0.0/PaddleOCR-VL-1.6_infer.tar
RUN mkdir -p /root/.paddlex/official_models \
    && cd /root/.paddlex/official_models \
    && wget -q "${MODEL_URL}" -O vl.tar \
    && tar -xf vl.tar \
    && mv PaddleOCR-VL-1.6_infer PaddleOCR-VL-1.6 \
    && rm -f vl.tar

ENV VLM_BACKEND=vllm
EXPOSE 8080
# genai_server が OpenAI 互換 API を :8080/v1 で出す（config の server_url が指す先）。
CMD ["/bin/bash", "-lc", "paddleocr genai_server --model_name PaddleOCR-VL-1.6-0.9B --host 0.0.0.0 --port 8080 --backend ${VLM_BACKEND}"]
