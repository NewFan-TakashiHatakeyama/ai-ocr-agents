# ベースライン

`production.json` は **実 AWS 環境で実測した**指標。次回以降の劣化判定はこれと比較する
（`scripts/aws_env.sh golden` が自動で読む）。

構成: PaddleOCR 3.7.0 / PP-StructureV3 / PP-OCRv6_medium / gemini-2.5-flash、
ゴールデン `golden/data/dev.jsonl`（公開サンプル 2 件 / 21 項目）。

**合格ラインではなく現状の記録**。数字が低いのは実態がそうだからで、正解を出力に
寄せて上げてはいけない。

## 履歴

| 日付 | タグ | exact_match | critical | precision | recall | stp_rate | harmful_rate |
|---|---|---|---|---|---|---|---|
| 2026-07-16 | v0.4.6 | **0.762**（16/21） | 0.800（4/5） | 0.800 | 0.762 | **0.500** | 0.000 |
| 2026-07-16 | v0.4.4 | 0.667（14/21） | 0.800（4/5） | 0.700 | 0.667 | 0.000 | 0.000 |

### v0.4.6: 和暦の略記に対応（+9.5pt）

`closing_date` / `payment_due` が `令02/01/31` のまま返っていた問題を正規化器側で
修正した（区切りを 年/月/日 に固定していたのが原因。詳細は
`packages/normalizers/src/newfan_normalizers/builtin.py`）。

この 2 件が確定したことで請求書が `needs_review` を抜け、`stp_rate` が 0.0 → 0.5 に
上がった。**精度の改善がそのまま人手確認の削減に効く**ことが実測で確認できた例。

## 残っている取りこぼし（5/21）

| 項目 | 正解 | 実際の出力 | 原因 |
|---|---|---|---|
| `issuer_name`（見積） | SystemBase | ＡＡＡ食品株式会社ＡＡＡ支社 御中 | 発行元がロゴ画像で OCR が読めず、宛先を拾っている |
| `customer_person` | 青田晴美 | 青田晴美様 | 敬称が落ちていない |
| `customer_postal` | 222-0001 | T222-0001 | 〒 を T と誤認したまま（type=string で正規化なし） |
| `customer_address` | …エイビービル | …エイピービル | OCR の字形誤認（ビ↔ピ） |
| `bank_accounts` | 3 口座 | 3 口座（区切りが違う） | 複数値の正準形が未定義。スキーマに配列型が要る |

`critical_exact_match` が 0.800 に留まるのは `issuer_name`（見積）だけが原因。
ロゴ画像から発行元名を読むには VL フォールバックか、ロゴ→社名の対応表が要る。

`customer_person` / `customer_postal` はスキーマの型選択の問題で、正規化器ではなく
`golden/data/schemas.json` 側（`person_name` / `postal_code` 型の追加）で解くべきもの。

`harmful_rate` が 0.0 なのは補正が正解を壊していないため（補正自体は動いている）。
