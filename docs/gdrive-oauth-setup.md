# Google Drive 実 OAuth セットアップ手順（⑤⑥ 実 E2E）

モックファースト実装の切替手順。コード側は完成しており、ここで取得した値を
`.env` に置くだけで FakeGDriveProvider → GoogleDriveProvider（実 Drive API v3）に
切り替わる。**スコープは drive.readonly（読取専用）のみ**。

## 1. Google Cloud Console での準備（ユーザー操作・約5分）

1. https://console.cloud.google.com/ で対象プロジェクトを選択（無ければ新規作成）
2. **API とサービス → ライブラリ** で「Google Drive API」を検索し **有効にする**
3. **API とサービス → OAuth 同意画面**
   - User Type: **外部**（Workspace 組織内のみなら「内部」でも可）
   - アプリ名・メールを入力して保存（公開ステータスは「テスト」のままで良い）
   - **テストユーザー** に、監視したい Drive を持つ Google アカウントを追加
4. **API とサービス → 認証情報 → 認証情報を作成 → OAuth クライアント ID**
   - アプリケーションの種類: **デスクトップアプリ**
   - 作成後に表示される **クライアント ID / クライアントシークレット** を控える
5. 監視したい Drive フォルダを開き、URL 末尾の **フォルダ ID** を控える
   （`https://drive.google.com/drive/folders/<この部分>`）

## 2. リフレッシュトークンの取得（ヘルパーが自動化）

リポジトリのルートで:

```bash
.venv/Scripts/python.exe scripts/setup_gdrive_oauth.py --client-id <ID> --client-secret <SECRET> --folder <フォルダID> --write-env
```

- ブラウザが開くので、**手順1-3で追加したテストユーザー**でログインして許可する
- `--folder` を渡すと GoogleDriveProvider と同じクエリで疎通を実測して件数を表示する
- `--write-env` で `.env` に以下が追記される（既存キーは触らない）:
  - `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET`
  - `GDRIVE_REFRESH_TOKEN`
  - `GDRIVE_FAKE_ROOT=`（**空文字 = Fake 無効・実 API モード**。compose は
    `${VAR-default}` 展開なので空文字がそのまま効く）

> `.env` は CRLF。手で追記する場合は改行を分けること（`echo >>` は前行に連結して壊れる）。

## 3. 切替と E2E 確認

```bash
docker compose -f deploy/compose.yaml --env-file .env up -d orchestrator-worker
```

1. worker ログに `gdrive: GoogleDriveProvider 有効` が出ること（Fake の行が消える）
2. Web の **接続管理** で GDrive 接続を作成:
   - フォルダID = 手順1-5 の実フォルダ ID
   - secret_ref = `env:GDRIVE_REFRESH_TOKEN`
3. **今すぐ同期** → 最終同期が「✓」になり、状態が **テスト済** に昇格
4. GDrive トリガーのワークフローを有効化し、**実際の Drive フォルダに PDF/PNG を置く**
   → ポーリング間隔（ローカル既定15秒）以内に自動取込 → 抽出 → レビューキューに載る

失敗した場合は接続一覧の「✗ 失敗」の tooltip（last_sync_error）と
`docker compose logs orchestrator-worker | grep gdrive` を確認。

## 補足

- **本番（AWS）** では `.env` でなく ECS タスク定義の環境変数/Secrets Manager に
  同じキーを置く（リフレッシュトークンは Secrets Manager 推奨:
  `ai-ocr/<env>/conn/<tenant>/gdrive-token` を作り secret_ref にその名前を渡す）
- M365 / Box も同型: `M365_TENANT_ID/M365_CLIENT_ID/M365_CLIENT_SECRET`（Graph の
  client credentials） / `BOX_CLIENT_ID/BOX_CLIENT_SECRET/BOX_ENTERPRISE_ID`（CCG）を
  設定し `M365_FAKE_ROOT=` / `BOX_FAKE_ROOT=` で切替。m365 の folder_id は
  `<drive_id>/<item_id>` 形式
- トークンを失効させたい場合は https://myaccount.google.com/permissions から
  アプリのアクセスを取り消す
