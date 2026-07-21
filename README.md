# NewFan AI-OCR エージェント (MVP)

テンプレートレス AI-OCR エージェントサービスの実装リポジトリ。詳細設計は
[`NewFan_AI-OCRエージェント詳細設計書_v1.2.md`](NewFan_AI-OCRエージェント詳細設計書_v1.2.md)、
PaddleOCR 適合調査は [`NewFan_PaddleOCR適合調査報告_v1.0.md`](NewFan_PaddleOCR適合調査報告_v1.0.md) を参照。

本番プラットフォームは **Amazon ECS**（Fargate 中心。GPU 推論のみ ECS on EC2）。
設計書 §2.3 の EKS 想定からの変更点は [ADR-0003](docs/adr/0003-deploy-target-ecs.md) を参照。

## リポジトリ構成

設計書 §15 に準拠。`ai-ocr-agents` をモノレポルートとし、`PaddleOCR/` は
推論エンジンのベンダ参照（公式リポジトリのクローン）として同居する。

```
ai-ocr-agents/
├─ packages/
│  ├─ schemas/          # ドメインモデル・API型（Pydantic）§4.2 / §5.5
│  ├─ paddle_client/    # PaddleOCR サービングクライアント＋応答型 §5.3 / 付録C-1,C-3
│  ├─ normalizers/      # 正規化器レジストリ §5.6
│  └─ validators/       # 決定論バリデータ V-* §5.7.3
├─ services/
│  ├─ gateway/          # FastAPI REST/認証/HITL API §6
│  ├─ orchestrator/     # LangGraph 抽出グラフ §4
│  ├─ ingest/           # 取込・ページ分割・前処理 §5.1 / §5.2（DD-01）
│  ├─ memory/           # 修正メモリ/ルール §5.8
│  └─ export/           # JSON/CSV/Webhook 配信 §5.9
├─ inference/
│  ├─ structure/        # PP-StructureV3 サービング設定（PP-OCRv6_medium 固定）
│  ├─ ocr/              # PP-OCRv6 単体（return_word_box=True 固定）
│  └─ vl/               # PaddleOCR-VL-1.6 サービング
├─ web/                 # HITL 検証UI（Next.js 15）§8
├─ prompts/2026.07-1/   # プロンプトバンドル（YAML）§4.6
├─ db/                  # Alembic マイグレーション（§7 DDL/RLS）
├─ deploy/              # compose / ECS(Terraform)
├─ scripts/             # fixture録画等のユーティリティ
├─ docs/adr/            # 設計判断記録（ADR）
└─ PaddleOCR/           # ベンダ参照（推論エンジン。編集しない・.gitignore）
```

## 開発

Python は uv ワークスペース（`requires-python >=3.12`）。

```bash
uv sync                       # 依存解決・仮想環境作成
uv run pytest packages        # パッケージのユニット/契約テスト
uv run mypy packages          # 型チェック（strict）
```

## 実装状況（MVP）

| コンポーネント | 状況 |
|---|---|
| packages/schemas | ドメインモデル実装済み |
| packages/paddle_client | 応答型・クライアント・span 抽出・契約テスト実装済み（fixture は実サービング録画で置換予定） |
| packages/normalizers | §5.6 正規化器レジストリ実装済み（string/date/money_jpy/number/tax_rate_jp/reg_no/bank） |
| packages/validators | §5.7.3 V-* 実装済み（法人番号チェックディジット含む。V-DUP は dup_lookup 注入待ち） |
| inference/* | サービング設定 YAML・compose 実装済み |
| deploy/ecs | ECS(Option A) Terraform IaC 実装済み（terraform validate は要 CI 実行） |
| services/ingest | スケルトン（検証ロジックは実装、rasterize/preprocess は Protocol） |
| services/orchestrator | 抽出グラフ。決定論＋LLM(kie/correct)＋memory(lookup/learn)＋structure_ocr＋vl_fallback を実体化。全ノード配線済み（load_context/apply_feedback 等の DB 接続は本番実装待ち） |
| services/gateway | §6 REST/HITL API（FastAPI）実装済み。In-Memory で E2E、本番は PgRepository(RLS)/Redis/HTTP 注入 |
| services/llm_adapter | §4.6 KIE/補正実装済み。span検証・DD-10強制・JSONリペア。既定 claude-opus-4-8 |
| services/memory | §5.8 修正メモリ/ルール実装済み。埋め込み(e5)・FAISS・learn・ルール自動検証(§5.8.4)。本番はe5/faiss extra |
| services/export | §5.9 JSON/CSV/Webhook 配信実装済み。HMAC署名・SSRFガード・指数リトライ。本番S3はboto3 extra |
| db | §7 DDL/RLS の Alembic 初期マイグレーション（offline SQL 生成で検証済み） |
| web | §8 HITL 検証UI スキャフォールド（SCR-02/03・レビューキュー・DocViewer/FieldPanel/ConfirmBar）。要 pnpm install |
