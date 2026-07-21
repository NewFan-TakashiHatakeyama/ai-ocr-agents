# マイグレーション実行イメージ（ECS one-off task）。ビルドコンテキスト = リポジトリルート。
#   docker build -f deploy/docker/migrate.Dockerfile -t newfan-migrate .
# 実行: DATABASE_URL=postgresql+psycopg://... で `alembic -c db/alembic.ini upgrade head`。
# マイグレーションは raw SQL（models 非依存）なので workspace パッケージは不要。
#
# ただし本イメージは alembic だけでなく「DB のセットアップ全般を所有者権限で行う場所」
# でもある（scripts/aws_env.sh が run-task で任意のスクリプトを流す）。§7.3 でアプリを
# 所有者でないロールに移したため、CREATE を伴う作業は全部ここに集約される:
#   - scripts/ensure_app_role.py     アプリ用ロールと GRANT
#   - scripts/setup_checkpointer.py  langgraph のチェックポイント表（langgraph が要る）
ARG UV_VERSION=0.9
ARG PYTHON_VERSION=3.12

FROM ghcr.io/astral-sh/uv:${UV_VERSION}-python${PYTHON_VERSION}-bookworm-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
RUN uv pip install --system alembic "psycopg[binary]>=3.1" "sqlalchemy>=2.0" \
    "langgraph-checkpoint-postgres>=2.0"
RUN useradd -m -u 10001 app
COPY --chown=app:app db ./db
USER app
CMD ["alembic", "-c", "db/alembic.ini", "upgrade", "head"]
