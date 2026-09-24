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

依存の入れ方と実行は CI の `e2e` ジョブと同じにする。ルートの `newfan-ocr` は workspace のメンバーに
依存していないので、素の環境では `uv run` だけでは（`--with` で redis・psycopg を足しても）
`newfan_*` が入らず import で落ちる。先に `uv sync --all-packages --all-extras` で全メンバーと
extras（redis・psycopg・langgraph は各サービスの `runtime` / `graph` extra）を入れ、
`uv run --no-sync` で実行する。

```bash
docker ps -q --filter name=orchestrator-worker   # 何も出なければ停止中（-q が無いとヘッダ行は必ず出る）

uv sync --frozen --all-packages --all-extras     # リポジトリ（または worktree）のルートで

DATABASE_URL=postgresql+psycopg://newfan:newfan@localhost:5433/newfan \
REDIS_URL=redis://localhost:6380 \
uv run --no-sync python scripts/e2e_real.py
```

- `uv sync` は実行したディレクトリの `.venv` に入れる。worktree で実行すれば worktree 側に `.venv` が
  でき、editable install も worktree のソースを指す。
- Windows（Git Bash）では出力が cp932 になって文字化けするので、読むときは `PYTHONIOENCODING=utf-8` を
  付ける（合否には影響しない）。
- dev の DB・Redis の中身に触れたくないときは、使い捨ての DB に CI と同じくマイグレーションを当て、
  Redis は空の論理 DB を指す（例: `/9`）。上の `uv sync` の後で:

  ```bash
  uv run --no-sync python -c "import psycopg; psycopg.connect('postgresql://newfan:newfan@localhost:5433/newfan', autocommit=True).execute('CREATE DATABASE e2e_tmp')"
  TMP=postgresql+psycopg://newfan:newfan@localhost:5433/e2e_tmp
  DATABASE_URL=$TMP uv run --no-sync alembic -c db/alembic.ini upgrade head
  DATABASE_URL=$TMP REDIS_URL=redis://localhost:6380/9 uv run --no-sync python scripts/e2e_real.py
  ```

  後片付けは `DROP DATABASE e2e_tmp`（別の DB に繋いで実行）と、Redis の論理 DB 9 の `FLUSHDB`。

uv sync をせず、**既に `uv sync --all-packages --all-extras` 済みの venv**（通常は main checkout の
`.venv`）の python で動かすこともできる。前提:

- redis・psycopg・langgraph・sqlalchemy などサードパーティの依存は、その venv に入っているものを使う。
  `PYTHONPATH` が補うのは `newfan_*` のソースだけなので、venv が `--all-packages --all-extras` で
  sync されていないと import で落ちる。ブランチで依存を足した・変えたときも venv には入っていないので、
  上の uv の手順にする。
- venv の `newfan_*` は editable install（site-packages の `.pth`）で main checkout のソースを指す。
  `PYTHONPATH` の並びは `.pth` の追加分より前に `sys.path` に入るので、worktree のルートで各 `src` を
  並べれば worktree のソースが import される。

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
