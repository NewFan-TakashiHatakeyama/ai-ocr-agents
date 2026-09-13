# 位置ガード（§5.5）の shadow 実測 ── Phase 5 有効化の判断材料（2026-09-13）

<!-- 設計 region-template-editor.md §9 Phase 5「shadow 実測に基づき許容パラメータを確定し
     REGION_GUARD_ENFORCE を on」の実測記録。2026-09-06 の 2 件では決められなかった
     誤検知率を、読取領域ヒント計測（region-field-add-and-hint-v2 §3）と同じ S1 / S2 / S3 の
     38 帳票で測った。 -->

## 何を測ったか

`golden/src/newfan_golden/region_guard_shadow.py`（`golden/scripts/run_region_arms.sh` と同じ
帳票・同じ領域）で、帳票ごとに 3 アームを 1 試行ずつ回し、`metrics.region.mismatch_fields` /
`layout_mismatch` を集めた。ガードのパラメータは現行値（許容 = ページ寸法の 5% と領域辺長の
50% の大きい方、doc レベル判定は領域 3 つ以上かつ過半）。

| アーム | 領域 | 何が分かるか |
|---|---|---|
| `aligned` | 計測セットの領域そのまま | S1 / S2: **誤検知**（正しい領域で mismatch が出る率）。S3: **別雛形の検知**（間違った領域で mismatch が出る率） |
| `shifted_all` | 全領域をページ高の 12% ずらす | 別レイアウト（全項目ずれ）の検知 |
| `shifted_one` | 1 領域だけ 12% ずらす | 1 項目だけ誤って引いた領域の検知（per-field レビューが増える経路） |

セットは読取領域ヒント計測と同じ（設計 region-field-add-and-hint-v2 §3）:
**S1** = その帳票自身の正解位置（21 件）、**S2** = 同じ雛形の別の帳票から転用した領域（5 件）、
**S3** = 目視で別雛形と判定した組から転用した領域（6 組 12 件）。ずらし方向はページ端で反転し
矩形の高さを保つ（前回の計測でページ下端の領域が 422 になった穴を塞いだ）。

出力: `golden/out/phase5/{s1,s2,s3}_guard_shadow.json`。

## 結果

### 誤検知（S1 / S2 の aligned）

| セット | run | 判定対象 field | mismatch field | 率 | mismatch のある run | doc レベル `layout_mismatch` |
|---|---|---|---|---|---|---|
| S1 | 20（※） | 108 | 3 | **2.8%** | 2 / 20 | **0 / 20** |
| S2 | 5 | 27 | 1 | **3.7%** | 1 / 5 | **0 / 5** |
| 合計 | 25 | 135 | 4 | **3.0%** | 3 / 25 | **0 / 25** |

※ S1 の sample7 は aligned の抽出が failed（捨てる）で判定対象に入っていない。

誤検知 4 件の内訳: `total_amount` × 3（S1 sample21・sample9、S2 sample21）、`document_no` × 1
（S1 sample21）。いずれも**同じ値・同じ種類の文字が紙面に複数ある項目**で、KIE が領域と別の
出現（小計／税込の合計、番号の再掲）を根拠にした形。領域の座標が悪いのではない。

### 検知（ずらしたアーム）

| セット | アーム | mismatch field / 判定対象 | run（mismatch あり） | doc レベル `layout_mismatch` |
|---|---|---|---|---|
| S1 | shifted_all | 107 / 109（98.2%） | 21 / 21 | 20 / 21（漏れ 1 件は判定対象 2 項目で下限 3 に満たない） |
| S1 | shifted_one | 25 / 110 | 19 / 21（ずらした項目を検知） | 1 / 21（sample21: 誤検知 2 ＋ 真 1 で 3/5 が過半） |
| S2 | shifted_all | 27 / 27 | 5 / 5 | 5 / 5 |
| S2 | shifted_one | 7 / 27 | 5 / 5 | 0 / 5 |
| S3 | shifted_all | 48 / 58（82.8%） | 12 / 12 | 12 / 12 |
| S3 | shifted_one | 37 / 57 | 12 / 12 | 7 / 12 |

### 別雛形の検知（S3 の aligned ＝ 間違ったテンプレートをそのまま当てる）

| 判定対象 field | mismatch field | 率 | mismatch のある run | doc レベル `layout_mismatch` |
|---|---|---|---|---|
| 57 | 40 | **70.2%** | 11 / 12 | **7 / 12** |

doc レベルに掛からなかった 5 件のうち 4 件は per-field の mismatch は出ている
（sample17←12: total_amount、sample13←2: document_date・document_no、sample29←27: 3 項目、
sample←6: 2 項目）。残り 1 件（sample12←17）は mismatch 0 ── 別雛形でも 5 項目が同じ位置に
あった組で、読取領域ヒント計測でも「別雛形でも一部の項目は同じ位置にある」と見えていた組。

## 有効化するとどうなるか（`REGION_GUARD_ENFORCE=1`）

`confidence_gate_node` は、doc レベル `layout_mismatch` **でない** run の mismatch field に
「設定領域外の位置で検出」の ReviewItem を足す（doc レベルで別レイアウトと判定した run は
per-field レビューを出さず metrics とバッジに留める。取引先 B の帳票を全件レビュー化させない
ため）。値・確信度は変えない。

- **コスト**（正しい領域の帳票）: 25 run 中 3 run に計 4 件の「要確認」が増える（run あたり 0.16 件）。
  doc レベルの誤判定は 0 なので「全項目レビュー」や「除外の見送り」の誤発火は起きない。
- **効き目**: 1 項目だけ誤って引いた領域は 26 run 中 24 run で検知（S1 19/21・S2 5/5）。
  別雛形の帳票は 12 run 中 7 run が doc レベル、4 run が per-field で見え、見えないのは
  1 run（項目が同じ位置にある組）。

## 判断

- 許容パラメータは**現行値のまま**（5% / 50% / 過半 / 下限 3）で有効化して良い水準:
  誤検知 field 3.0%、doc レベル誤判定 0 / 25。下限 3 を 2 に下げると shifted_all の漏れ
  1 件は拾えるが、shifted_one の sample21 のような「誤検知 2 ＋ 真 1」が doc レベルに化ける
  経路が広がるので下げない。
- 有効化は環境変数だけで済む（コードの既定は off のまま）: compose は `.env` に
  `REGION_GUARD_ENFORCE=1`、ECS は tfvars に `region_guard_enforce = "1"`（Terraform 変数と
  ecs.tf の配線は PR #19 で追加済み）。**まず dev / staging で on にして「設定領域外の位置で検出」
  の件数を見る**のを推奨する。誤検知が `total_amount` に集中している（3 / 4）ので、実運用で
  同じ傾向なら、金額項目だけ許容を広げる（または「同じ値が領域内にもある」ときは mismatch に
  しない）改良を次に置く。
- `REGION_EXCLUDE_SKIP_ON_LAYOUT_MISMATCH`（PR #19）は位置ガードとは独立の KIE 前判定なので、
  この計測の対象外。除外領域のある帳票が貯まってから `metrics.region.skipped_exclude_pages` で
  測る。

## 計測の限界

- 各アーム 1 試行。LLM の揺れは読取領域ヒント計測（5 試行）より大きく効く。誤検知 4 件は
  いずれも「別の出現を根拠にした」形なので試行で入れ替わり得るが、率の桁（数%）は動かない
  と見る。
- 領域は計測用に機械生成（S1 は実検出位置、S2 / S3 は転用）。人が手で引いた矩形の粗さは
  含まれていない。許容 5% / 50% はその分の余裕として置いてある。
- ずらし量は 12% の 1 水準。小さなずれ（1 行分＝2〜3%）の検知は測っていない（許容の内側なので
  設計上 mismatch にしない）。

## 再実行

```bash
# セットごとに 1 回（S2 は数分、S1 / S3 は 10〜20 分）。compose の gateway / worker が動いていること。
# --regions は読取領域ヒント計測（run_region_arms.sh）が出力した領域ファイルをそのまま使う
for ARM in s2 s3 s1; do
  PYTHONPATH=golden/src PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m newfan_golden.region_guard_shadow \
    --gold golden/data/region_ab_${ARM}.jsonl --regions golden/out/phase4r4/c1/${ARM}_regions_full.json \
    --api http://localhost:8000/v1 --token "$TOKEN" --trials 1 --shift 0.12 \
    --out golden/out/phase5/${ARM}_guard_shadow.json
done
```
