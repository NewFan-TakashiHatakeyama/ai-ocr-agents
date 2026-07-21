# tests/ — E2E・結合テスト（§14.1）

## E2E: 抽出パイプライン一気通し

`e2e/test_pipeline_e2e.py` は **ingest → 抽出グラフ(§4, 実 LangGraph) → export(§5.9)** を一本で通す。
外部境界（GPU サービング / クラウド LLM）だけを Fake にし、それ以外（検証・span 構築・正規化・
confidence・validate・gate・memory・canonical JSON・Webhook 署名）は実装本体を通す。

- structure-svc は `FakeStructureClient` が `/layout-parsing` 応答を返す（GPU 不要）
- LLM は `FakeProvider` が KIE の JSON を返す（API キー不要）
- 実 LangGraph（`build_graph`）で quality_gate/条件分岐/finalize までルーティングを検証

```bash
uv run pytest tests/e2e          # langgraph は dev group に含む
```

> このテストは実際に file:// のページ画像を読むため、`file_uri_loader` の実経路を検証する
> （合成 fixture だけでは通らなかった Windows の file:// パス変換バグをこの E2E が検出した）。

## 実サービングに対する契約テスト固定（付録C-1/C-3）

`packages/paddle_client/tests/fixtures/*.json` は現状**合成 fixture**（プレースホルダ）。
実サービングの応答で置換すると契約テストが実データで固定される。

```bash
# 1. 推論サービングを起動（正しい PaddleX サービングイメージが必要）
docker compose -f deploy/compose.yaml up structure-svc ocr-svc

# 2. 代表帳票で応答を録画（fixtures を上書き）
uv run python scripts/record_fixtures.py --image sample.png \
    --structure-url http://localhost:8081 --ocr-url http://localhost:8082

# 3. 契約テストが実データで通ることを確認し、単語座標の実フィールド名を schema に確定
uv run pytest packages/paddle_client
```

### この環境で実サービングを起動できなかった理由

`deploy/compose.yaml` が参照する PaddleX サービングイメージ（`paddlex --serve` で `/layout-parsing`・
`/ocr` を公開）が入手できなかった（`:latest` タグは registry に存在せず、ローカルの
`ai-ocr-paddle-ocr` は別プロジェクトの GPU 用 FastAPI で契約が異なる）。実運用環境では、
PaddleOCR 公式のサービングイメージ（CPU=OpenVINO or GPU）を用意し `inference/*/pipeline_config.yaml`
をマウントして起動する。起動後は上記手順で fixtures を録画すれば契約テストが実データで固定される。
