# grounding の比較で空白類の違いを吸収する（2026-09-24）

<!-- 詳細設計 §5.7.2（confidence 算出式）の「value_normalized が source_quote の正規化文字列と
     一致」の**正規化文字列**を定めた記録。§5.7.2 本体は詳細設計書 v1.2（リポジトリ直下）にあり、
     docs/ に grounding を説明する文書が無いためここに置く。実装は
     services/orchestrator/src/newfan_orchestrator/confidence.py の `_norm`。
     同じ複数 span の値で見つかった §5.7.2 の ocr_conf の「対象span」の解釈も、後半の追記に残す
     （`evidence_ocr_confidence` / `evidence_source`）。 -->

## 何が起きたか

複数行にまたがる値（住所など）が、根拠 span があるのに **grounding 0・確信度 0.00** になり、
「根拠 span なし（強制レビュー）」に回っていた。

dev DB の document `doc_d9c967a2437a41a4a8a23a21`（sample2.png）での観測:

| run | 項目 | value_normalized | source_quote | grounding |
|---|---|---|---|---|
| `run_dda2fb931f3a4e8c8ed6dfc5`（スキーマなし） | recipient_address | `神奈川県横浜市港北区樽町エイピービル` | `神奈川県横浜市港北区樽町 エイピービル` | **0** |
| 同上 | issuer_address | `東京都新宿区四谷9-9-9サプライビル2F` | `東京都新宿区四谷9-9-9 サプライビル2F` | **0** |
| `run_ca1f3c11ca2b465a9c0e3877`（スキーマあり） | customer_address | `神奈川県横浜市港北区樽町 エイピービル`（value_raw は改行入り） | `神奈川県横浜市港北区樽町 エイピービル` | 1.0 |

根拠 span は 1 行 1 span（span 3「神奈川県横浜市港北区樽町」、span 4「エイピービル」）。

## 原因

1. **source_quote の空白は原文に無い。** kie（`newfan_llm_adapter.kie`）は根拠 span のテキストを
   **半角空白で**連結して source_quote を作る。改行で連結しているのではない。
2. **grounding は NFKC + strip だけで比べていた。** LLM が 2 行を**つないで**返すと
   （「…樽町エイピービル」）、根拠「…樽町 エイピービル」と一致せず、値の方が長いので部分一致
   （値 ⊂ 根拠）にもならず 0 に落ちる。
3. スキーマあり run が 1.0 だったのは、LLM が**改行で区切って**返し、string の正規化
   （`norm_string`）が改行を 1 つの半角空白に畳んだ結果、たまたま連結の空白と一致したため。
   LLM が行をどう区切るか（改行／空白／区切りなし）で grounding が 1.0 と 0 に分かれていた。

grounding の呼び出し元は抽出グラフの `confidence_score` ノード（`nodes.py`）だけで、スキーマ
あり・なし（ADR-0006）とも同じ経路を通る。スキーマなしでは型が無く全項目が string の正規化に
なるので、LLM の区切り方がそのまま効く。

address_jp（ADR-0007）の項目も同じ理由で落ちうる。規則 3 が日本語に接する空白を除くので、
値は「…樽町エイピービル」になり、span 連結の空白を含む根拠と食い違う。

## 決定: 比較形で空白類の違いを吸収する

grounding の一致・部分一致は、value_normalized と source_quote の両方を次の形にしてから比べる。

1. NFKC（全角空白 U+3000 も半角空白になる）
2. 空白類（改行・タブ・空白の連なり）は、**両隣が半角英数字のときだけ** 1 つの半角空白に畳み、
   それ以外（日本語の文字・記号に接するもの、先頭・末尾）は除く

段（1.00 / 0.85 / 0.70 / 0.00）と順序は変えない。VL 由来は比較の前に 0.7 を返す（DD-09）、
型変換は完全一致しなければ 0.85、部分一致は 0.7 のまま。比較形が変わるのは一致の判定だけ。

### 英数字どうしの間の空白を残す理由

空白を無条件に除くと、原文に無い値が「完全一致」になる。

- 根拠「12 500」（数量と単価の 2 span）に値「12500」── 金額の読み違いが 1.0 になる
- 根拠「丸の内1-1-1 3F」に値「1-1-13F」── 13 階／13 番地に化けた住所が一致する
- 根拠「A-123 Sample.BLD」に値「A-123Sample.BLD」

部分一致でも同じで、根拠「12 500」に値「2500」が 0.7 になる。英数字どうしの間は空白が語の
境界を担うので、種類・長さの違い（改行と空白、空白 2 つと 1 つ）だけを吸収して残す。
日本語の文字に接する空白は語の境界を担わない（「東京都 品川区」「9-9-9 サプライビル」）。

この線引きは住所の正規化（ADR-0007 規則 3）と同じにしてある。address_jp の値はすでにこの形
なので、根拠を同じ規則で畳めば一致する（線引きが違うと、正規化が空白を除いた値が根拠と
食い違う）。

### 誤って一致にしないもの（テストで固定）

- 数字・文字の違い: 「9-9-8」と「9-9-9」、「エイビービル」と「エイピービル」、「綱島」と「樽町」
- 部分文字列: 1 行目だけ（「東京都新宿区四谷9-9-9」）や後半だけ（「樽町エイピービル」）は 0.7 のまま
- 英数字の間の空白の有無: 上の 3 例と「03-9999-9999FAX:…」
- 行の境目の両側が英数字の値を区切りなしでつないだもの: 「…丸の内1-1-1JPタワー」「…梅田3-1-32F」
  （同じ規則の帰結。下の「残る課題」）

回帰テストは `services/orchestrator/tests/test_confidence.py`（関数単位）と
`services/orchestrator/tests/test_deterministic.py`（normalize → confidence_score の配線。
スキーマなしと address_jp）。

## dev DB での影響（読み取りのみで再判定）

`extraction_fields` のうち value_normalized と source_quote がある 258 行を、旧比較形
（NFKC + strip）と新比較形で判定し直した（型変換・VL の段は除く、一致／部分一致／不一致の分類のみ）。

| 旧 → 新 | 行数 |
|---|---|
| 一致 → 一致 | 97 |
| 部分一致 → 部分一致 | 109 |
| 不一致 → 不一致 | 41 |
| **不一致 → 一致** | **10** |
| **不一致 → 部分一致** | **1** |
| 一致・部分一致 → 下がる | 0 |

変わった 11 行はすべて span の区切りの空白だけが違う複数行の値（sample2 の宛先・発行者住所が
4 run で 7 行、「A AA食品株式会社AAA支社 御中」と根拠「A AA食品株式会社 AAA支社 御中」が 2 行、
「596-0006大阪府岸和田市春木若松町1026-56」と根拠「596-0006 大阪府岸和田市 春木若松町1026-56」が
1 行、部分一致の 1 行は「A AA食品株式会社AAA支社」）。「A AA」の英字どうしの空白は値と根拠の
両方に残るので、一致の妨げにならない。保存済みの run は再計算しないので、反映には再抽出が要る。

## 追記: 複数 span の値の ocr_conf を根拠 span 全体の最小にする（2026-09-24）

### 何が問題だったか

`confidence_score`（`nodes.py`）は ocr_conf を根拠 span の**先頭 1 つ**からしか取っていなかった。
複数行にまたがる値は 1 行 1 span なので、2 行目以降の読みが弱くても確信度に効かない
（sample2 の宛先住所: span 3 = 0.971、span 4 = 0.933 で確信度 0.971）。上の修正で grounding が
0 から 1.0 に戻ったため、この過大評価が確信度にそのまま出るようになった。

### 設計との照合

§5.7.2 は `ocr_conf = min(対象spanのchar_confs)  # 無ければ行conf`。「対象span」は field の根拠
span（`span_ids`）で、複数あるときに先頭だけを見る根拠は設計に無い（MVP 実装からの簡略化で、
経緯の記録も無い）。次の 2 点から、**根拠 span 全体の最小が設計どおり**と判断した。

- 式全体が min で組まれている（`confidence = min(ocr_conf, grounding)`、補正時も min）。
  確信度は値の中でいちばん弱いところで決める、が §5.7.2 の考え方。
- `span_ids` の並びは LLM の出力順で、読み順の保証も無い（region-template-editor.md の支配ページの
  項）。「先頭」は 1 行目とすら限らず、どの行の conf を採るかが LLM の出し方で変わっていた。

### 決定

- ocr_conf は根拠 span ごとに `ocr_confidence`（char_confs があればその最小、無ければ行 conf。
  1 span のときの従来の扱いと同じ）を取り、その**最小**（`confidence.evidence_ocr_confidence`）。
  1 span の値は従来と同じ値になる。
- 根拠 span が無い、または State に見つからない span id が混じるときは 0.0。従来も先頭 span が
  見つからなければ 0.0 だった。kie は State にある id しか残さないので実経路では起きない。
- **VL 由来の判定も根拠 span 全体で行う**（`confidence.evidence_source`。どれか 1 つが VL なら
  grounding 上限 0.7）。同じ「先頭だけ」の実装で、DD-09「VL 由来のフィールドは必ずレビューへ」が
  LLM の出力順しだいで破れていた（VL は OCR span を破棄せず併存させるので、OCR と VL の span を
  併せて根拠にした値がありうる）。

### dev DB での影響（読み取りのみで再判定）

1 span の値は変わらないので、`span_ids` が 2 つ以上の `extraction_fields` 26 行（12 run・9 帳票）を
対象にした。span は各 run の LangGraph checkpoint（`checkpoint_blobs` の `spans` チャネルの最新版）
から読み、`norm_meta`（type_converted・confidence_cap）と schema（critical・always_review_fields）も
同じ checkpoint から取った。旧式（先頭 span・旧比較形）で計算し直した確信度は保存値と 26/26 行で
一致した。

このブランチの grounding（空白類の吸収後）を固定し、ocr_conf を先頭 span → 根拠 span 全体の最小に
変えたときの確信度とゲート判定（always_review / grounding 0 / confidence < 閾値。critical 0.90・
standard 0.80）を比べた。

| 項目 | 行数 | 確信度（先頭 → 全体） |
|---|---|---|
| sample2 の宛先住所（span 3 / 4。同じ画像の別 run で 8 行） | 8 | 0.971 → 0.933 |
| sample2 の振込先口座（3 行の値、span 10〜12） | 3 | 0.949 → 0.924 |
| 宛名（sample.png・templateless_invoice.png） | 2 | 0.946 → 0.939 |
| sample7 の発行者住所 | 1 | 0.996 → 0.964 |
| sample7 の顧客住所（3 span） | 1 | 0.99996 → 0.99984 |
| 変わらない（grounding 0.7 で頭打ち 6 行、先頭 span がもともと最小 5 行） | 11 | — |

- **auto → review に変わる field は 0 件**。下がった 15 行はどれも critical ではなく、下がった後の
  最小は 0.924（standard 閾値 0.80 まで余裕がある）。llm_correct の起動条件（0.80 未満）を新たに
  踏む行も無い。
- 上の空白類の修正で review → auto に上がる複数 span の行（sample2 の住所など）は、ocr_conf を
  全体の最小にしても auto のまま（最小 0.933）。
- VL 由来の span は dev DB に 0 件（vl-svc は既定で無効、DD-15）なので、VL 判定の変更の影響も 0。
- dev DB は同じ帳票の再抽出が多く母数が小さい。2 行目以降の conf が閾値を下回る帳票は含まれて
  いないので、実運用ではそうした帳票が review に回るようになる（それが狙い）。

回帰テストは `test_confidence.py`（`evidence_ocr_confidence` / `evidence_source` の関数単位）、
`test_nodes.py`（2 行目の char_confs・後ろの VL span・見つからない span id が効くこと）、
`test_deterministic.py`（sample2 の宛先住所の形で確信度 0.933、2 行目が 0.72 なら review）。

## 残る課題

- **改行位置が半角英数字どうしの間にある複数行の値**を、LLM が行を区切りなしでつないで返すと
  grounding 0 のまま（「…丸の内1-1-1」＋「JPタワー」を「…丸の内1-1-1JPタワー」、「…梅田3-1-3」＋
  「2F」を「…梅田3-1-32F」）。根拠「12 500」に値「12500」を一致させないための**意図的な線引き**で、
  空白・改行で区切って返した値は一致する。span の境目の空白だけを吸収するには source_quote に
  span の境界を残す必要があり（今は通常の空白と区別できない）、kie の出力と保存列の意味が変わるので
  見送った。dev DB の 258 行に、空白を全部除けば一致・部分一致になるのに新比較形で不一致の行は
  無い（0 行）。挙動は `test_grounding_multiline_joined_at_alnum_boundary_stays_zero` で固定。
- llm_correct に渡す char_confs は先頭 span のまま（`llm_nodes.make_llm_correct`）。確信度には
  効かない（補正プロンプトの材料）が、複数 span の値では 2 行目以降の低確信文字がプロンプトに
  出ない。value_raw の文字と char_confs の対応を span をまたいで取る必要があるので別途。
- address_jp の規則 4（数字に挟まれた「ー」等を「-」に揃える）は空白ではないので吸収していない。
  OCR が「9ー9ー9」と読んだ根拠に値「9-9-9」は不一致（grounding 0）のまま。
- 部分一致は span の境目をまたいでも成り立つようになった（根拠「東京 都庁」に値「京都」が 0.7）。
  従来から部分一致は span 内の文字単位の包含で判定しており（「東京都庁」に「京都」も 0.7）、
  0.7 は閾値（standard 0.80 / critical 0.90）を下回ってレビューに回るので、同じ扱いとした。
- grounding 0 のレビュー理由は「根拠 span なし」だが、実際は「span はあるが値と一致しない」も
  含む（今回の不具合もその表示で出ていた）。
