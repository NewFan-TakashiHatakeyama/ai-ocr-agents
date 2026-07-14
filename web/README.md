# web — HITL 検証UI（§8）

Next.js 15 / React 19（App Router）。gateway-api（§6）に対する検証・レビュー UI。

> ⚠️ 本ディレクトリは**スキャフォールド**です。この環境では `pnpm` 未導入・依存未インストールのため
> ビルド/型チェック（`tsc`）は未実施。`pnpm install` 後に `pnpm dev` で起動します。

## 画面（§8.1）

| ルート | 画面 | 内容 |
|---|---|---|
| `/review` | SCR-03 レビューキュー（§8.5） | 優先度順の要確認一覧 |
| `/documents` | SCR-02 ドキュメント一覧 | status/種別/ページ数 |
| `/documents/[id]` | SCR-03 検証画面（§8.2） | 左=画像ビューア＋bboxオーバーレイ / 右=フィールドパネル / フッタ=確定バー |

## コンポーネント（§8.3）

- `DocViewer` — 前処理後 PNG（署名URL）＋ bbox オーバーレイ。座標変換
  `scale = renderedWidth / naturalWidth`（前処理後画像＝座標系の正, DD-01）。選択フィールドをハイライト。
- `FieldPanel` — 並び順 pending→conf昇順。conf帯は色分け（<0.6赤/<0.8黄/≥0.8緑/検証済み青）。行内編集。
- `ConfirmBar` — 残 pending 数、[全て確認して確定]。pending>0 は二段確認。編集を corrections として
  楽観ロック（result_version）付きで保存 → `/confirm`（グラフ resume）。

状態管理: サーバ状態＝TanStack Query、編集バッファ＝zustand（§8.3）。

## セットアップ

```bash
cd web
cp .env.example .env.local   # NEXT_PUBLIC_API_BASE, dev トークン
pnpm install
pnpm dev                     # http://localhost:3000
```

`NEXT_PUBLIC_API_BASE` は gateway（既定 http://localhost:8000/v1）。認証トークンは dev では
`NEXT_PUBLIC_DEV_TOKEN` または localStorage `nf_token`。本番はログインフロー（Auth基盤）で取得する。

## 未実装（TODO）

- 単文字差分ポップオーバー（CharDiffPopover, §8.3）、表エディタ（TableGridEditor, §8.6.2）
- 検証モード切替（field/text/table, §8.6）、キーボードショートカット（§8.4）
- ダッシュボード（SCR-04, STP率/精度/コスト）、ルール管理（SCR-05）、スキーマ管理（SCR-06）
- チャットUI（§3.3, SSE）、ワークフローノードエディタ（SCR-07, §16）
- 409 楽観ロック競合時の 3-way マージ提示（§8.3）
