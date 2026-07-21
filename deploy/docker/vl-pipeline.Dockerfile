# vl-svc（PaddleOCR-VL のパイプライン側, §5.4 / DD-09）。
#
# 構成は 2 コンテナに分かれる:
#   - vl-svc（本イメージ）  : paddlex --serve でレイアウト検出＋整形。VL 認識は genai へ委譲。
#   - vlm-server            : paddleocr genai_server（vLLM）。**GPU が要るのはこちらだけ**。
#
# 本イメージが CPU で足りるのは、VLRecognition が genai_config.backend=vllm-server で
# server_url 越しに委譲されるため（inference/vl/pipeline_config.yaml）。実測で確認済み。
# 公式 compose は api 側にも GPU を割り当てるが、レイアウト検出（PP-DocLayoutV2）は
# CPU で動く。GPU を 1 枚に集約でき、vlm-server だけ落とせば課金も止まる。
#
# 推論イメージ（structure/ocr と同じ）に genai-client プラグインだけ足す。
ARG BASE_IMAGE=newfan-inference:local

FROM ${BASE_IMAGE}

ARG PADDLEX_VERSION=3.7.2
# VL パイプラインは genai_client エンジンを使う。無いと起動時に
# RuntimeError: The genai client plugin is not available. で落ちる（実測）。
RUN pip install --no-cache-dir "paddlex[genai-client]==${PADDLEX_VERSION}"

# VL のレイアウトモデルを焼き込む（起動時 DL を避ける）。VL パイプラインは
# PP-DocLayoutV2 か PP-DocLayoutV3 しか受け付けず、3.7.2 に V3 は無いので V2。
RUN python -c "\
from paddlex import create_model; \
create_model('PP-DocLayoutV2')" || echo '[warn] PP-DocLayoutV2 の事前取得に失敗（起動時に取得する）'

ENV PIPELINE_CONFIG=/opt/inference/vl/pipeline_config.yaml
