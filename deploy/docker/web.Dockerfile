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
# .dockerignore で除外しているが、二重に防ぐ。.env.local には dev の JWT
# （NEXT_PUBLIC_DEV_TOKEN）が入っており、Next.js は NEXT_PUBLIC_* をビルド時に
# 公開 JS バンドルへ焼き込む。実際に .next/static へ JWT が混入していた。
RUN rm -f .env.local .env*.local

ARG NEXT_PUBLIC_API_BASE
ENV NEXT_PUBLIC_API_BASE=${NEXT_PUBLIC_API_BASE}
# standalone 出力（next.config.mjs）。node_modules 全部を運ぶと 1.5GB 超になる。
RUN npm run build

# バンドルに「署名まで揃った JWT」が入っていないことを確認してから先へ進む
# （混入したまま公開すると URL を知る誰もが API を叩ける。実際に .env.local 経由で
#  .next/static へ混入していた）。ヘッダだけの文字列（UI の説明文など）に反応すると
# 誤検知になるので、payload と signature が続く形だけを弾く。
RUN if grep -rEq 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}' .next/static; then \
      echo '[FATAL] 公開バンドルに JWT が混入しています。.env.local を除外してください。' >&2; \
      exit 1; \
    fi

FROM node:${NODE_VERSION}-slim AS runtime
WORKDIR /app
ENV NODE_ENV=production PORT=3000
RUN useradd -m -u 10001 app
# standalone は必要な依存だけを含む。static / public は別途コピーが要る（Next の仕様）。
COPY --from=build --chown=app:app /app/.next/standalone ./
COPY --from=build --chown=app:app /app/.next/static ./.next/static
# public/ はロゴ（/logo-full.png）の配信元。standalone に含まれないため明示コピーが要る。
# 忘れると本番だけロゴが 404 になり、ローカルの dev サーバでは再現しない。
COPY --from=build --chown=app:app /app/public ./public
USER app
EXPOSE 3000
CMD ["node", "server.js"]
