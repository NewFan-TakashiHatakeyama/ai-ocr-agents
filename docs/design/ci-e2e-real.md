# 実 PG + Redis の E2E を CI に載せる（scripts/e2e_real.py、2026-09-24）

<!-- 残タスク D。scripts/e2e_real.py はこれまで手元の compose（postgres:5433 / redis:6380）に
     当てて手で回すだけで、CI（.github/workflows/ci.yml）には載っていなかった。
     ここには CI への組み込み方の判断と、「E2E が落ちたら CI が赤になる」ことの確認結果を残す。 -->

## 背景

`scripts/e2e_real.py` は外部境界（GPU の structure-svc / クラウド LLM）だけを Fake にし、
DB（PostgreSQL）・キュー（Redis Streams）・チェックポイント・本番アダプタは実物を通す E2E。

| Phase | 通す経路 |
|---|---|
| A 自動確定 | gateway の `RedisQueue.enqueue`（XADD）→ q.extract を consumer group で消費 → worker → `extraction_fields` / `extraction_tables` 保存・`confirmed` → gateway `PgRepository.get_run` で読み戻し |
| B HITL | 低信頼で `needs_review` 停止 → gateway `QueueOrchestratorClient.resume` が resume ジョブを XADD → worker が `Command(resume=…)` → 修正値で `confirmed` |
| C export | A/B の finalize が積んだ q.export を export worker が消費 → canonical JSON（`run_a.json`） |
| D 除外領域 | `exclude_regions` 付きスキーマ → span/セルの決定論除外 → `metrics.region`・集約 ReviewItem で `needs_review`・F-0 の bbox・`resolve_regions` |

CI の pytest は実 PG の結合テスト（`test_pg_*_integration.py`）まではあるが、キューは InMemory で、
**Redis Streams を挟んだ「gateway → worker → Pg → q.export → export worker」の貫通**はどのテストも
通っていなかった。この経路が壊れても CI は緑のままだった。

## 判断

### 1. quality ジョブに足さず、別ジョブ `e2e` にする

- quality は ruff → mypy → pytest の順に走り、先のステップが落ちると後ろは実行されない。
  同じジョブに置くと、lint の指摘 1 件で E2E の成否が見えなくなる。
- pytest の Pg 結合テストと DB・Redis を共有しない。E2E は tenant `ten_e2e` の行を毎回
  DELETE → INSERT で作り直すので、同じ DB に他のテストがいると干渉の余地が生まれる。
- 別ジョブは並列に走るので、壁時計時間はほぼ増えない。代償は `uv sync` とマイグレーションが
  もう 1 回走ること（`uv.lock` が同じなので setup-uv のキャッシュは共有される）。

### 2. サービスとマイグレーションは quality と揃える

- postgres サービスは quality と同一定義（`postgres:16` / newfan / 5432）。redis は
  `deploy/compose.yaml` と同じ `redis:7` を足し、`redis-cli ping` のヘルスチェックで起動を待つ。
- 依存は quality と同じ `uv sync --frozen --all-packages --all-extras`（worker/export/gateway の
  `runtime` extra に redis・psycopg・langgraph が入っている）。
- マイグレーションは quality の「Apply migrations」ステップと同一（空の DB に
  `alembic -c db/alembic.ini upgrade head`）。ci.yml を yaml.safe_load で読んで、2 つのステップが
  同じ内容であること・既存 4 ジョブ（quality / golden / web / images）が変わっていないことを確かめた。

### 3. 合否はスクリプトの終了コードだけで決める

- CI のステップは `uv run --no-sync python scripts/e2e_real.py` を直接実行する。`| tee` のような
  パイプは挟まない。`shell:` 未指定の run は `bash -e {0}`（pipefail なし）で動くので、
  パイプの右側が成功すると E2E の失敗が消えてしまう。
- スクリプト側の終了コードの約束（`scripts/e2e_real.py` の docstring にも記載）:
  - 全 Phase（A〜D）が PASS のときだけ 0、それ以外は 1。
  - Phase の途中で例外が出たら、トレースバックを出してその Phase を FAIL にし、残りの Phase も
    続けて実行する。最初の例外で後続の成否が見えなくなるのを避けるためで、握りつぶして PASS には
    しない。
  - Phase 関数の戻り値は `True` そのものだけを PASS とみなす（`return` の書き忘れで `None` が
    返っても PASS に倒れない）。記録された Phase が A〜D の 4 つ揃っていなければ全体を FAIL にする。
- `PYTHONUNBUFFERED=1` を付ける。print（stdout）と Phase 内の例外のトレースバック（stderr）の
  順序を CI のログで崩さないため。
- `timeout-minutes: 20`。スクリプト内の待ちは consume の `block=1000ms` と export の最大 500 周
  （空になれば抜ける）だけで、手元では import 済みの状態で 1 回 2 秒程度で終わる。何かが固まった
  場合に既定の 6 時間待たないための上限（`uv sync` を含むジョブ全体の上限）。

### 4. スクリプトの修正（CI に載せるにあたって見つかったもの）

1. **Phase B の表示と終了コードのずれ**: 「extract で `needs_review` に止まること」の確認が
   全体の成否（`ok`）には入っていたが、Phase B の成否（`phase_ok["B"]`）には入っていなかった。
   止まらずに通過した場合、`phases: B PASS` なのに `E2E RESULT: FAIL` という食い違ったログになる。
   各 Phase を関数に分け、その戻り値だけから Phase の成否と全体の終了コードを出すようにした。
2. **スキーマ版の衝突による不定期の失敗**: `field_schemas` は `(tenant_id, doc_type, version)`
   が UNIQUE だが、版を `abs(hash(sch)) % 1000` で決めていた。str の hash はプロセスごとに乱数化
   されるので、3 つのスキーマ（sch_a / sch_b / sch_d）の版が同じ値になると IntegrityError で落ちる
   （空の DB でおよそ 0.3%。前回の残骸がある手元では、残っている別 id の版とも衝突し得るので
   およそ 0.6%）。CI では「たまに赤になる」事象になるので、版を固定値（1 / 2 / 3）にした。
3. **ジョブを取れなかったときの診断**: A/B で q.extract から目的のジョブを 1 件も取れなかった場合
   （compose の orchestrator-worker に横取りされた等）に、その旨を `[FAIL]` として出力する。
   以前も status の確認で FAIL にはなっていたが、理由がログから読めなかった。

Phase の中身（seed・assert の内容）は変えていない。

### 5. 手元の実行手順を CI と揃える（レビュー指摘、2026-09-24）

- 手元の手順（`tests/README.md` とスクリプトの docstring）は
  `uv run --with "psycopg[binary]" --with redis python scripts/e2e_real.py` のままだった。ルートの
  `newfan-ocr` は workspace のメンバーに依存していない（`[build-system]` も無い）ので、`uv run` が
  入れるのはルートの dev group と `--with` の分だけで、`newfan_*` は入らない。素の環境（`.venv` の
  無い worktree）で実際に `ModuleNotFoundError: No module named 'newfan_gateway'` になることを確かめた。
  下の「確認したこと」の手元の実行は venv＋`PYTHONPATH` で行っていたので、書いてあった uv の手順
  自体は通していなかった（`uv sync --all-packages --all-extras` 済みの `.venv` があれば通るが、
  それは手順に書かれていない前提）。
- CI と同じ `uv sync --frozen --all-packages --all-extras` → `uv run --no-sync python scripts/e2e_real.py`
  に揃えた。手元と CI で依存の入り方が同じになり、「手元では通るが CI で import に落ちる」
  （またはその逆）の差が出ない。
- venv＋`PYTHONPATH` の手順は、`uv sync` をもう一度せずに main checkout の `.venv` を使い回す手段として
  残し、前提を書き直した: サードパーティの依存はその venv のもの（`--all-packages --all-extras` で
  sync 済みであること）で、`PYTHONPATH` が補うのは `newfan_*` のソースだけ。`PYTHONPATH` は
  editable install の `.pth` の追加分より前に `sys.path` に入るので worktree のソースが勝つ。
  以前の「worktree ではこの方法にする」は、worktree で `uv sync` すれば worktree 側に `.venv` ができて
  editable install も worktree を指すので、必須ではなくなった。
- worker の停止確認を `docker ps --filter name=orchestrator-worker`（「何も出なければ停止中」）から
  `docker ps -q --filter name=orchestrator-worker` に直した。`-q` が無いと一致する container が
  無くてもヘッダ行（`CONTAINER ID   IMAGE ...`）が必ず出るので、書いてあるとおりに読むと
  「動いている」と誤読する。`-q` なら停止中は何も出ない。
- dev の DB・Redis の中身に触れずに回す手順（使い捨て DB にマイグレーション＋Redis の空の論理 DB）も
  README に足した。下の「CI 相当」の確認はこの形で行っている。

## 確認したこと

手元（Windows、venv の python に worktree の各 `src` を `PYTHONPATH` で通して実行）:

| 条件 | 結果 |
|---|---|
| dev DB（newfan、alembic 0008）＋ dev Redis（6380/0）、orchestrator-worker 停止 | A〜D PASS、終了コード 0（修正前・修正後とも） |
| CI 相当: 使い捨て DB に `alembic upgrade head` を当てた直後＋空の Redis 論理 DB（6380/9） | 1 回目・2 回目（作り直しの再実行）とも A〜D PASS、終了コード 0。Phase C の processed=2（run_a / run_b のみ） |

故障注入（使い捨て DB/Redis で、スクリプトをモジュールとして読み込んで差し替え）:

| 注入 | Phase の結果 | 終了コード |
|---|---|---|
| Fake LLM が別の金額（999999）を返す | A FAIL / B PASS / C FAIL / D FAIL | 1 |
| Phase A の本体が例外を投げる | A FAIL（トレースバック出力）/ B PASS / C FAIL / D PASS。B 以降も実行される | 1 |
| Phase D が `None` を返す | D FAIL | 1 |
| Redis に繋がらない（`redis://localhost:1`） | A・B・C FAIL（ConnectionError）/ D PASS（D は Redis に書かない経路） | 1 |
| `REDIS_URL` 未設定 | 起動時に KeyError | 1 |

いずれの失敗でも 0 は返らない（= CI のジョブは赤になる）。CI 上での初回実行は、この変更を push して
確認する（ここでは push していない）。

判断 5 の手順（README どおり）での確認（Windows、`.venv` の無い worktree から）:

| 手順 | 結果 |
|---|---|
| 旧手順 `uv run --frozen --with "psycopg[binary]" --with redis python -c "import newfan_gateway.prod"` | `.venv` を作って dev group だけ入れ、`ModuleNotFoundError: No module named 'newfan_gateway'`（終了コード 1） |
| `docker ps -q --filter name=orchestrator-worker` | 何も出ない（停止中）。`-q` 無しではヘッダ行だけが出ることも確認 |
| `uv sync --frozen --all-packages --all-extras` | worktree の `.venv` に 128 パッケージ（uv のキャッシュから 11 秒程度）。`uv.lock` は変わらない |
| 使い捨て DB に `uv run --no-sync alembic -c db/alembic.ini upgrade head` → 空の Redis 論理 DB（6380/9）で `uv run --no-sync python scripts/e2e_real.py` | A〜D PASS、終了コード 0。Phase C の processed=2。`PYTHONIOENCODING` 無しでも終了コードは 0（出力が cp932 で文字化けするだけ） |

venv＋`PYTHONPATH` の手順は、main checkout の `.venv` の python で `newfan_gateway` /
`newfan_orchestrator` が worktree の `src` から import されること（`PYTHONPATH` 無しでは main checkout
の `src`）を確かめた。

## 手元での実行

`tests/README.md`「実 PG + Redis の E2E」を参照（依存の入れ方と実行は CI と同じ
`uv sync --frozen --all-packages --all-extras` → `uv run --no-sync`。判断 5）。compose の orchestrator-worker が
動いていると q.extract のジョブを横取りされて A/B が落ちるので、
`docker ps -q --filter name=orchestrator-worker` が何も出さない（停止中）ことを確かめてから実行する。
