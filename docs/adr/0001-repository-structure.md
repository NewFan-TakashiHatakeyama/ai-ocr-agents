# ADR-0001: リポジトリ構成（ai-ocr-agents をモノレポルートとする）

- 状態: Accepted
- 日付: 2026-07-14
- 関連: 詳細設計 §15、DD-11

## コンテキスト

詳細設計 §15 は monorepo（uv workspaces + pnpm）を規定し、`newfan-ocr/` を例示する。
一方、実リポジトリは `ai-ocr-agents` であり、推論エンジンの参照実装 `PaddleOCR/`
（公式リポジトリのクローン）が既に同居している。

## 決定

`ai-ocr-agents` をそのままモノレポルートとし、`packages/` `services/` `inference/`
`prompts/` `deploy/` `docs/` を配置する。`PaddleOCR/` はベンダ参照として残し、
アプリコードからは編集しない（サービング設定・応答スキーマ・ベンチの一次資料として参照）。

uv ワークスペースの members は `packages/*` と `services/*` に限定し、`PaddleOCR/`
（独自 pyproject を持つ）をワークスペースに取り込まない。

## 影響

- Python パッケージ名は `newfan-*`（import 名 `newfan_*`）で統一。
- `PaddleOCR/` は `.git` を内包する（真の submodule ではない）。CI では参照専用。
