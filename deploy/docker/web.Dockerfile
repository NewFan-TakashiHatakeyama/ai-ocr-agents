# web（Next.js 15 / SCR-01〜）イメージ。ビルドコンテキスト = リポジトリルート。
#   docker build -f deploy/docker/web.Dockerfile --build-arg NEXT_PUBLIC_API_BASE=... -t ai-ocr-web .
#
# 注意: NEXT_PUBLIC_* は**ビルド時**にクライアントバンドルへ焼き込まれる。実行時の env では
# 変わらないため、API のベース URL は build-arg で渡す（ALB の DNS 名が確定してからビルドする）。
#
# NEXT_PUBLIC_DEV_TOKEN は**渡さない**。JWT が公開 JS バンドルに入り、ALB は
# インターネットに公開されているため、URL を知る誰もが API を叩けてしまう。
# 認証は利用者がブラウザの localStorage["nf_token"] に入れる（lib/api.ts が優先して読む）。
ARG NODE_VERSION=22

FROM node:${NODE_VERSION}-slim AS build
WORKDIR /app
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
ARG NEXT_PUBLIC_API_BASE
ENV NEXT_PUBLIC_API_BASE=${NEXT_PUBLIC_API_BASE}
# standalone 出力（next.config.mjs）。node_modules 全部を運ぶと 1.5GB 超になる。
RUN npm run build

FROM node:${NODE_VERSION}-slim AS runtime
WORKDIR /app
ENV NODE_ENV=production PORT=3000
RUN useradd -m -u 10001 app
# standalone は必要な依存だけを含む。static / public は別途コピーが要る（Next の仕様）。
COPY --from=build --chown=app:app /app/.next/standalone ./
COPY --from=build --chown=app:app /app/.next/static ./.next/static
USER app
EXPOSE 3000
CMD ["node", "server.js"]
