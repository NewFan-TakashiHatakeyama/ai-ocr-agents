# gateway-api イメージ（§6）。ビルドコンテキスト = リポジトリルート。
#   docker build -f deploy/docker/gateway.Dockerfile -t newfan-gateway .
# 本番 ASGI: uvicorn newfan_gateway.main:app（DATABASE_URL/REDIS_URL で本番配線）。
ARG UV_VERSION=0.9
ARG PYTHON_VERSION=3.12

FROM ghcr.io/astral-sh/uv:${UV_VERSION}-python${PYTHON_VERSION}-bookworm-slim AS build
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
# workspace 全体（path 依存の解決に必要）。.dockerignore が PaddleOCR/web 等を除外。
COPY pyproject.toml uv.lock ./
COPY packages ./packages
COPY services ./services
RUN uv sync --frozen --no-dev --package newfan-gateway --extra runtime

FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime
WORKDIR /app
# editable workspace install（.venv の .pth が src を指す）ため src ごとコピー。
COPY --from=build /app /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
RUN useradd -m -u 10001 app && chown -R app /app
USER app
EXPOSE 8000
# ヘルスチェックは ALB/ECS 側の /healthz を使用。
CMD ["uvicorn", "newfan_gateway.main:app", "--host", "0.0.0.0", "--port", "8000"]
