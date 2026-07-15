/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // コンテナ配布用。node_modules ごと運ぶと 1.5GB 超になるため、
  // 必要な依存だけを .next/standalone にまとめる（deploy/docker/web.Dockerfile が使う）。
  output: "standalone",
};

export default nextConfig;
