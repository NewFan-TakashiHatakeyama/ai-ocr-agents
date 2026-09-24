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

## 実 PG + Redis の E2E（`scripts/e2e_real.py`）

上の E2E が InMemory で通す経路を、**実 PostgreSQL と Redis Streams** で通す（GPU / LLM だけ Fake）。
gateway の enqueue（XADD）→ worker の消費 → Pg 保存 → q.export → export worker の貫通を見る。

- Phase A 自動確定 / B HITL（needs_review → resume ジョブ）/ C export（canonical JSON）/
  D 除外領域（metrics.region・F-0 bbox）。中身は `scripts/e2e_real.py` の docstring。
- **終了コード**: 全 Phase PASS のときだけ 0、それ以外は 1。Phase 内の例外もその Phase の FAIL として
  1 になる（残りの Phase は続けて実行して結果を出す）。
- **CI**: `.github/workflows/ci.yml` の `e2e` ジョブ。空の postgres:16 に quality と同じ手順で
  `alembic upgrade head` を当て、redis:7 と一緒に本スクリプトを直接実行する（パイプを挟まない）。
  判断の経緯と故障注入での確認結果は [`docs/design/ci-e2e-real.md`](../docs/design/ci-e2e-real.md)。

手元では compose の postgres（5433）と redis（6380）に当てる。tenant `ten_e2e` の
doc_a/b/d・run_a/b/d・sch_a/b/d を毎回消して作り直す。**compose の orchestrator-worker が
動いていると q.extract のジョブを横取りされて A/B が落ちる**ので、止まっていることを確かめてから実行する。

```bash
docker ps --filter name=orchestrator-worker   # 何も出なければ停止中

DATABASE_URL=postgresql+psycopg://newfan:newfan@localhost:5433/newfan \
REDIS_URL=redis://localhost:6380 \
uv run --with "psycopg[binary]" --with redis python scripts/e2e_real.py
```

uv を使わない（venv の python で動かす）場合は、各パッケージの `src` を `PYTHONPATH` に並べる
（worktree で動かすときも、editable install が main checkout を指すのでこの方法にする）。
Windows の Git Bash で、リポジトリ（または worktree）のルートから:

```bash
P=""; for d in services/*/src packages/*/src golden/src; do P="$P;$(pwd -W)/$d"; done
PYTHONPATH="${P#;}" PYTHONIOENCODING=utf-8 \
DATABASE_URL=postgresql+psycopg://newfan:newfan@localhost:5433/newfan \
REDIS_URL=redis://localhost:6380 \
<main checkout>/.venv/Scripts/python.exe scripts/e2e_real.py
```

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
