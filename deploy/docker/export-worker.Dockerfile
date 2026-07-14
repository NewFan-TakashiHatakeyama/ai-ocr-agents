# export-svc 常駐ワーカーイメージ（§5.9 / §9）。ビルドコンテキスト = リポジトリルート。
#   docker build -f deploy/docker/export-worker.Dockerfile -t newfan-export-worker .
# 起動: python -m newfan_export.worker_main（q.export を消費し canonical JSON 配信）。
# runtime は sqlalchemy/psycopg/redis/boto3 のみ（torch 非依存で軽量）。
ARG UV_VERSION=0.9
ARG PYTHON_VERSION=3.12

FROM ghcr.io/astral-sh/uv:${UV_VERSION}-python${PYTHON_VERSION}-bookworm-slim AS build
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
COPY packages ./packages
COPY services ./services
RUN uv sync --frozen --no-dev --package newfan-export --extra runtime

FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime
WORKDIR /app
COPY --from=build /app /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
RUN useradd -m -u 10001 app && chown -R app /app
USER app
# 必須 env: DATABASE_URL, REDIS_URL / 任意: EXPORT_S3_BUCKET, EXPORT_S3_KMS_KEY_ID, EXPORT_STORAGE_ROOT
CMD ["python", "-m", "newfan_export.worker_main"]
