# orchestrator-svc 常駐ワーカーイメージ（§2.1 / §9）。ビルドコンテキスト = リポジトリルート。
#   docker build -f deploy/docker/orchestrator-worker.Dockerfile -t newfan-orchestrator-worker .
# 起動: python -m newfan_orchestrator.worker_main（q.extract を消費）。
#
# 注意: runtime extra は e5 埋め込み（sentence-transformers→torch, DD-06）を含み画像が大きい。
# 埋め込みを別サービス化する場合は EMBED_EXTRA=graph でスリム化し memory を degrade できる。
ARG UV_VERSION=0.9
ARG PYTHON_VERSION=3.12
ARG EMBED_EXTRA=runtime

FROM ghcr.io/astral-sh/uv:${UV_VERSION}-python${PYTHON_VERSION}-bookworm-slim AS build
ARG EMBED_EXTRA
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
COPY packages ./packages
COPY services ./services
# プロンプトバンドル（§4.6 / 付録A）。llm_adapter の default_bundle_dir が
# prompts/{version} を上方探索するため、イメージに無いと worker が起動時に
# FileNotFoundError: prompts/2026.07-1 で落ちる（実コンテナで検出）。
COPY prompts ./prompts
RUN uv sync --frozen --no-dev --package newfan-orchestrator --extra ${EMBED_EXTRA}

FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime
WORKDIR /app
COPY --from=build /app /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
# /data は gateway が書いたページ画像を worker が読む共有領域（file:// image_uri）。
# gateway と同じ uid で所有させる（S3_BUCKET 運用時は未使用）。
RUN useradd -m -u 10001 app && chown -R app /app \
    && mkdir -p /data && chown app /data
USER app
# 必須 env: DATABASE_URL, REDIS_URL, STRUCTURE_URL / 任意: VL_URL, LLM_MODEL, ANTHROPIC_API_KEY
CMD ["python", "-m", "newfan_orchestrator.worker_main"]
