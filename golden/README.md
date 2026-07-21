# ゴールデンセット回帰（§14.2 / DD-03）

本番昇格の条件。**実際に動いている系**に既知の正解つき帳票を流し、精度が前回より
落ちていないかを機械で判定する。

## なぜ Fake で測らないか

推論も LLM も差し替えれば CI だけで完結して速いが、それは「差し替えた物の精度」を
測っているだけで、本番に出す判断材料にならない。ここでは実 PaddleOCR・実 LLM を通す。

CI（`.github/workflows/ci.yml` の `golden` ジョブ）が守るのは別の範囲:

- 指標定義とゲート判定が壊れていないこと（`golden/tests`）
- ゴールデンデータが読めること・スキーマと critical が食い違っていないこと

## 指標（§14.2）

| 指標 | 定義 |
|---|---|
| `exact_match` | 項目単位の完全一致（**正規化後**の値で比較） |
| `critical_exact_match` | critical 項目だけの完全一致 |
| `precision` | 予測した（値がある）項目のうち正しかった割合 |
| `recall` | 正解項目のうち拾えた割合 |
| `stp_rate` | 人手確認なしで確定できた文書の割合 |
| `harmful_rate` | **補正が正しい値を壊した率**。分母は補正が働いた件数 |

リリースゲート:

- 有害率が **0.1% 以上**でブロック（`HARMFUL_RATE_LIMIT`）
- ベースライン比で加重平均が **-0.5pt 超**の劣化でブロック（`REGRESSION_LIMIT_PT`）
- ベースライン比で有害率が悪化したらブロック

加重平均は `0.5 * exact_match + 0.5 * critical_exact_match`。critical を壊す方が実害が
大きいため半分の重みを割いている。

## 実行

```bash
# 1. スキーマを投入（一度だけ。down するたびに DB ごと消えるので都度必要）
scripts/aws_env.sh seed-schemas

# 2. 実系に流して測り、ゲートを判定する
scripts/aws_env.sh golden
```

`out/golden/metrics.json` に今回の指標が出る。これを
`golden/baselines/production.json` に置くと、次回から劣化判定の基準になる。

個別に回す場合:

```bash
# 収集（実系にアップロード → 抽出 → 結果を取得）
uv run --extra collect --package newfan-golden python -m newfan_golden.collect \
  --gold golden/data/dev.jsonl --api http://<alb>/v1 --token "$JWT" --out out/pred.jsonl

# 採点＋ゲート（終了コード: 0=通過, 1=劣化/有害率超過, 2=入力不正）
uv run python -m newfan_golden.cli \
  --gold golden/data/dev.jsonl --pred out/pred.jsonl \
  --baseline golden/baselines/production.json --out out/metrics.json
```

## データ

`golden/data/dev.jsonl` は公開サンプル帳票 2 件（`sample.png` / `sample2.png`）。
値は**人が画像を読んで起こした正解**で、OCR/KIE の出力ではない。出力を正解にすると
間違ったまま固定され、回帰を検出できなくなる。

本番のゴールデンセットは顧客帳票なのでリポジトリには置かない。S3 の専用バケットで
バージョン管理し、`image_uri` を手元へ落としたパスに書き換えて `collect` に渡す。

`schema_id` は必須。無いと `load_context` が空スキーマを返し、KIE が 1 項目も抽出せず
「全項目 Recall 0」という無意味な結果になる（`collect` は事前に弾く）。

`golden/data/schemas.json` の critical は `dev.jsonl` の critical と一致させること。
片方だけ直すと `critical_exact_match` が実態とずれる。CI の
`golden/scripts/check_critical.py` が突き合わせる。
