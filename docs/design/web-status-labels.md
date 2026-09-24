<!-- 画面設計書 v1.0 §2 STATUS CHIPS（documents.status と1:1）の補足。チップの表を
     documents 以外の status にも流用していたために、表に無い値が英語の生値で出ていた件。
     前提: docs/design/region-template-editor.md §3.1（再抽出と supersede_review）、
     docs/design/16-agent-workflow.md（ワークフローの status と実行履歴）。 -->

# 設計: ステータス表示の語彙 ── 表をテーブルごとに分け、サーバの語彙と突き合わせる

2026-09-24。

## 0. 何が起きていたか

web のステータスチップ（`StatusChip` / `lib/fields.ts` の `statusChip`）は
**documents.status の表 1 つ**しか持っていなかった（画面設計書 §2「documents.status と1:1」）。
ところが呼び出し側はこれを documents 以外の status にも使っていて、表に無い値は
「生値のまま・灰色（st-uploaded）」に落ちていた。

| 表示箇所 | 渡していた値 | 表に無かった値 | 画面での見え方（修正前） |
|---|---|---|---|
| 検証画面の見出し（`/documents/[id]`） | `GET /documents/{id}/result` の `status` ＝ **extraction_runs.status** | `superseded` | `superseded`（灰色） |
| ダッシュボード「処理内訳（Run n件）」 | `GET /metrics` の `status_counts` ＝ **extraction_runs の集計** | `superseded` | 行名が `superseded`、バーは既定の灰色 |
| ワークフロー一覧・エディタの見出し | **workflows.status** | `draft` / `active` / `paused` / `retired`（全部） | `draft` / `active` 等の英語（すべて灰色） |

根本原因は「status の語彙はテーブルごとに別物なのに、表示の表が 1 つで、どの表で
引くかを呼び出し側が選べなかった」こと。型も `status: string` なので、どこで何を
渡しても型検査を通っていた。

dev DB（2026-09-24 時点、読み取りのみ）でも実際に出る値だった:
extraction_runs に `superseded` 4 件、workflows に `active` 3 件・`draft` 2 件。

## 1. 洗い出し（サーバの語彙と web の表示箇所）

語彙の正本は**マイグレーションの CHECK 制約**（DB はこれ以外を受け付けない）。

| テーブル.列 | 取りうる値（CHECK） | 書き手 | web の表示箇所 | 修正前 |
|---|---|---|---|---|
| documents.status | uploaded / queued / processing / needs_review / in_review / confirmed / exported / failed | gateway・worker | 帳票一覧、サイドバーの「最近のドキュメント」 | 全値対応済み |
| extraction_runs.status | processing / needs_review / confirmed / failed / **superseded** | gateway（supersede）・worker | 検証画面の見出し、ダッシュボードの内訳 | **superseded が未対応** |
| workflows.status | draft / active / paused / retired | gateway（activate / pause。retired は現状書かない） | ワークフロー一覧・エディタ | **全値未対応** |
| workflow_runs.status | running / waiting_hitl / succeeded / failed / skipped | orchestrator の射影 | 実行タブ（`lib/workflowRuns.ts`） | 全値対応済み |
| workflow_node_runs.status | pending / running / succeeded / failed / skipped | orchestrator の射影 | 実行ドロワー（同上） | 全値対応済み |
| tenant_rules.status | draft / validating / active / retired | gateway | ルール管理（ページ内の表） | 全値対応済み |
| connections.status | （CHECK なし）untested / tested / active / disabled | gateway・worker | 接続管理（ページ内の表） | 全値対応済み |

チップにしていない status は対象外: jobs.status（`useExtractJob` が分岐に使うだけ）、
extraction_fields.review_status（フィールドの並びと色に使う）、
connections.last_sync_status（ok / error の 2 分岐）。

## 2. 決めたこと

| # | 決定 | 理由 |
|---|---|---|
| D1 | 表を **`web/lib/statusLabels.ts` に種類（kind）ごとに分けて置き**、`StatusChip` は `kind`（`document` / `extractionRun` / `workflow`）を**必須**にする | どの表で引くかを呼び出し側に明示させる。省略可にすると「とりあえず document」で同じ事故が再発する。純粋関数に切り出して vitest で全値を検査できるようにする |
| D2 | superseded は「**再抽出で置き換え済み**」、色は新設の **st-inactive**（塗り無し・灰色の枠線）。チップの title に「確定できません」を添える | 失敗ではないので赤にしない。処理待ち（灰色の塗り）とも見分けたい。gateway は superseded の確定を E1005 で断るので、その事情を補足で見せる |
| D3 | run の共通 4 値（processing / needs_review / confirmed / failed）は documents と**同じ見た目** | worker は run の遷移を documents に写す（`UPDATE documents SET status = :st`）。同じ状態が画面によって別の色になると読み手が迷う |
| D4 | ワークフローは 下書き（灰）/ 有効（緑）/ 停止中（琥珀）/ 退役（st-inactive） | 「有効」「退役」はルール・接続の画面と同じ言葉。paused は止める操作のボタン名「停止」に合わせ、自動実行されていないことが目に付くよう琥珀にする |
| D5 | 未知の値は**生値のまま中立色**で出す（落とさない） | gateway が先に語彙を増やしても画面が壊れない。`lib/workflowRuns.ts` の `runStatusView` と同じ方針 |
| D6 | ダッシュボードの内訳バーの色もチップのクラスから引く（`statusBarColor`）。ページ内の独自の色表は消す | 表を 2 つ持つと片方だけ直す。旧い色表は documents の値（exported / in_review）を持ち、run に出る superseded を持っていなかった |
| D7 | vitest（`statusLabels.test.ts`）が**マイグレーションの CHECK を読んで**、web の一覧と過不足なく一致すること・全値に日本語の表示名があること・色のクラスが globals.css に定義されていることを確かめる。ワークフロー実行（workflow_runs / workflow_node_runs）の表も同じテストで突き合わせる | 表を直しても、次に語彙を足したときにまた漏れる。サーバの定義から機械で引けば、マイグレーションだけ直した PR が web のテストで落ちる。版番号順に読んで後の版を勝たせ、downgrade() は読まない（旧い定義に戻す側なので）。CHECK が 1 つも読めなければ落とす（パーサが壊れたまま素通りしない） |
| D8 | スキーマ画面の「確定」チップは `StatusChip` を通さず直に書く（見た目は従来どおり） | スキーマの版の状態は documents.status の語彙ではない。隣の「アーカイブ済み」チップと同じ書き方にそろえる |

型の側でも、`DOCUMENT_STATUSES` 等の一覧から表の型（`Record<…Status, …>`）を作るので、
一覧に値を足して表を直し忘れると tsc が落ちる。一覧とサーバの突き合わせは D7 のテスト、
一覧と表の突き合わせは型、の二段にしている。

## 3. 残す課題

- **検証画面の「確定」ボタンは superseded / failed の run でも押せる**。押すと gateway が
  E1005（409）で断るが、画面に出るのは 409 共通の「他のメンバーが先に更新しました」で、
  「置き換え済みだから確定できない」という理由そのものは出ない。今回はラベルと色
  （チップの title の補足を含む）に限った。ボタンを塞ぐなら run status の判定を
  `useSchemaSaved` の `canRerun` と同じく lib に寄せて行う。
  なお検証画面が superseded を表示するのは、supersede と新 run 作成の間の窓か、
  新 run の作成が失敗したときに限られる（`get_latest_run` は開始時刻の新しい順）。
  普段 superseded が目に入るのはダッシュボードの内訳。
- ルール管理・接続管理の表はページ内に残したまま（全値対応済みのため今回は動かしていない）。
  tenant_rules には CHECK があるので、`statusLabels.ts` へ寄せれば D7 の突き合わせに載せられる。
- `lib/types.ts` の `WorkflowDto.status` は `"draft" | "active" | "paused"` で retired を含まない
  （gateway が retired を書かないため実害は無い）。

## 4. レビューと反映（2026-09-24）

| 重大度 | 指摘 | 反映 |
|---|---|---|
| low | `isKnownStatus` の JSDoc が「表に**無い** status か」と、実装（表に有れば true）と逆のことを書いている | 「表に有る（既知の）status か。未知の値は statusView が生値のまま中立色で出す」に直す |
| （見直しで追加） | ワークフローの draft / paused の補足（チップの title）が「自動実行されません」で、手動なら動くように読める。実際は gateway が active 以外の手動実行（`POST /workflows/{id}/runs`）も E1005 で断り、トリガーも active だけを引く（`list_active_workflows`） | 「有効にするまで／停止中は **新しい実行が始まりません**（トリガー・手動実行とも）」に直す。「実行されない」としないのは、走行中の run と失敗した run の再実行（retry）は停止・再保存（新版は draft に戻る）の後も止まらないため。補足の語をテストで押さえる |

同じファイルのほかのコメント（CHECK と 1:1、retired を gateway が書かない、run の状態が
documents に写される、未知の値の扱い、内訳バーの灰色落ち）は実装・サーバと突き合わせて
食い違いが無いことを確かめた。
