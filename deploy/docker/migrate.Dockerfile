# マイグレーション実行イメージ（ECS one-off task）。ビルドコンテキスト = リポジトリルート。
#   docker build -f deploy/docker/migrate.Dockerfile -t newfan-migrate .
# 実行: DATABASE_URL=postgresql+psycopg://... で `alembic -c db/alembic.ini upgrade head`。
# 迁移は raw SQL（models 非依存）なので workspace パッケージは不要。
ARG UV_VERSION=0.9
ARG PYTHON_VERSION=3.12

FROM ghcr.io/astral-sh/uv:${UV_VERSION}-python${PYTHON_VERSION}-bookworm-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
RUN uv pip install --system alembic "psycopg[binary]>=3.1" "sqlalchemy>=2.0"
RUN useradd -m -u 10001 app
COPY --chown=app:app db ./db
USER app
CMD ["alembic", "-c", "db/alembic.ini", "upgrade", "head"]
