# ベースライン

`production.json` は **実 AWS 環境で実測した**指標。次回以降の劣化判定はこれと比較する
（`scripts/aws_env.sh golden` が自動で読む）。

## 2026-07-16 初回計測（v0.4.4）

| 指標 | 値 |
|---|---|
| exact_match | 0.667（14/21） |
| critical_exact_match | 0.800（4/5） |
| precision | 0.700 |
| recall | 0.667 |
| stp_rate | 0.000 |
| harmful_rate | 0.000 |

構成: PaddleOCR 3.7.0 / PP-StructureV3 / PP-OCRv6_medium / gemini-2.5-flash、
ゴールデン `golden/data/dev.jsonl`（公開サンプル 2 件 / 21 項目）。

**これは合格ラインではなく現状の記録**。7 件の取りこぼしの内訳は下記で、
数字が低いのは実態がそうだからで、正解を出力に寄せて上げてはいけない。

### 取りこぼしの内訳（7/21）

| 項目 | 正解 | 実際の出力 | 原因 |
|---|---|---|---|
| `issuer_name`（見積） | SystemBase | ＡＡＡ食品株式会社ＡＡＡ支社 | 発行元がロゴ画像で OCR が読めず、宛先を拾っている |
| `closing_date` | 2020-01-31 | 令02/01/31 | **和暦が正規化されていない**（type=date なのに未変換） |
| `payment_due` | 2020-02-29 | 令02/02/29 | 同上 |
| `customer_person` | 青田晴美 | 青田晴美様 | 敬称が落ちていない |
| `customer_postal` | 222-0001 | T222-0001 | 〒 を T と誤認したまま（type=string で正規化なし） |
| `customer_address` | …エイビービル | …エイピービル | OCR の字形誤認（ビ↔ピ） |
| `bank_accounts` | 3 口座 | 3 口座（区切りが違う） | 複数値の正準形が未定義。スキーマに配列型が要る |

`stp_rate` が 0.0 なのは、2 件とも低信頼項目があり `needs_review` で止まったため。
`harmful_rate` が 0.0 なのは補正が正解を壊していないため（補正自体は動いている）。

`closing_date` / `payment_due` の和暦未変換は§5.6 の正規化器の穴で、
`customer_person` / `customer_postal` はスキーマの型選択の問題。いずれも実測して初めて
見えたもので、直したら再計測してこのベースラインを更新する。
