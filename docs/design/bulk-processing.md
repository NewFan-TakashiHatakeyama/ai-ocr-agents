<!-- 帳票登録フロー（ADR-0006: 取込 → 自動発見 → テンプレート化 → 同種を大量に流す）の
     最後の工程。region-field-add-and-hint-v2 §0 で「複数同時アップロードと一括再処理は
     別件」としていたもの。前提: docs/design/region-template-editor.md §3.1（再抽出ボタン・
     supersede_review）、ADR-0006「抽出の自動開始はしない」。 -->

# 設計: 大量処理の工程 ── 複数ファイル同時アップロードと一括再抽出

2026-09-12。

## 0. 何が足りなかったか

テンプレート化までは 1 通の帳票で完結するが、その後の「同種の帳票を大量に流す」が
手作業だった。

| 操作 | 従来 | 何が困るか |
|---|---|---|
| アップロード | 1 ファイルずつ（file input も D&D も先頭 1 件だけ取る） | 30 通の請求書を 30 回ドラッグする |
| 抽出開始 | 帳票ページを開いて「抽出を開始」 | 30 通なら 30 回ページを開く |
| 定義を直した後の取り直し | 保存トーストの「この帳票を再抽出」（1 通だけ） | 既に取り込んだ同種の帳票は 1 通ずつ開いて再抽出 |

「抽出の自動開始はしない」（ADR-0006、LLM コストが掛かる操作は明示クリック）は
維持する。一括投入は**利用者が件数を見て押す**操作にし、静かに走らせない。

## 1. 決めたこと

| # | 決定 | 理由 |
|---|---|---|
| D1 | 一括の抽出開始は**新しいエンドポイント** `POST /documents/extract-batch` に置き、単体 `/extract` の判定本体を `_start_extract` に切り出して共有する | 単体と一括で「何が拒否されるか」がずれると、片方だけ確定済みを置き換える事故になる。判定は 1 箇所 |
| D2 | 帳票ごとの拒否（不在・競合・確定済み・スキーマなし）は **skipped に理由付きで載せて続行**し、一括全体は 202 | 200 件のうち 1 件が確定済みなだけで残り 199 件が止まるのは使えない。他テナントの id や不在 id も 404 にせず skipped（存在を漏らさない） |
| D3 | **確定済み（confirmed / exported）は supersede_review に依らず置き換えない**（true でも false でも E1005）。doc_type 指定の既定の母集合にも入れない（uploaded / needs_review / failed） | region-template-editor §3.1 の再抽出と同じ。会計連携済みの値を無警告で置き換えない。当初は判定が `supersede_review` の分岐の中にしかなく、既定（false）の一括投入が確定済みを queued に落としていた（レビューで発見） |
| D4 | 上限 200 件。id 指定で超えたら E1003、doc_type 指定は**新しい順に 200 件**で切って `truncated: true` | LLM を回す件数の歯止め。呼び出し側が繰り返せば全件になる |
| D5 | `schema_id` 省略時は**帳票の種別の最新版**（`admin.get_schema`）。種別なし・定義なしは `no_schema` で skipped（スキーマレス抽出には落とさない） | 一括は「定義を直したので取り直す」用途で、自動発見に落ちると領域・除外が黙って no-op になる（§3.1 D15 と同じ懸念） |
| D6 | アップロード後の抽出は**チェックボックス（既定 on、種別を選んだときだけ有効）**で、1 通ずつ `extract-batch` を `document_ids` 1 件・`schema_id` 省略で呼ぶ（**使う版はサーバが帳票の種別から最新版を解く**。D5） | 種別が決まっていれば使う定義は明らかで、押す手間だけが残る。未指定（自動発見）は従来どおり帳票ページで開始。ADR-0006 の「自動開始しない」は**利用者が種別と一緒に選ぶ**形で守る。当初は `GET /doc-types` のキャッシュ（staleTime 5 分）の `schema_id` を単体 `/extract` に渡していたが、テンプレートを直して 5 分以内に戻ると**旧版で全通が走る**（単体 `/extract` の `schema_id` 省略は自動発見なので使えない） |
| D7 | 一覧の再抽出は確認ダイアログを挟み、「レビュー待ちの結果も置き換える（supersede）」を**既定 on** で見せる | 一括の主な用途はテンプレート化後の取り直しで、典型状態は needs_review。既定 off だと全件 E1005 で skipped になる。確定済みはチェックに関係なく置き換えない（D3） |
| D8 | 一覧に queued / processing の帳票がある間は **5 秒ごとに再取得**し、無くなれば止める | ジョブごとのポーリング（`useExtractJob`）は 1 通用。200 件ぶん張ると API を叩きすぎる |
| D9 | **確定処理中（documents.status = in_review）と、他の利用者がソフトロック中の帳票も置き換えない**（supersede_review に依らず E1005、`reason` は `in_review` / `locked`）。単体 `/extract` も同じ | confirm は documents を in_review にして resume を投げるだけで、run は worker が finalize するまで needs_review のまま。この窓で旧 run を superseded に落とすと worker が superseded を confirmed に進めて会計連携まで流し、その確定値が新 run の後ろに隠れる（削除の `get_delete_blocker` と同じ理由）。ロックは §8.2 の助言的ロックだが、確認中の帳票を横から置き換えると入力中の修正が引き継がれず、相手の確定が E1005 で止まる（削除と同じく他者のロックを尊重する。自分のロックは通す） |

## 2. API

### `GET /documents`（拡張）

| クエリ | 意味 |
|---|---|
| `doc_type` | 完全一致 |
| `status`（繰り返し可） | `?status=uploaded&status=failed` は OR。1 値の従来の呼び方は不変 |
| `cursor` / `limit` | 従来どおり（Pg は created_at 降順・id 降順のキーセットに直した。以前は id 降順＝実質ランダム） |

### `POST /documents/extract-batch`（新規、role uploader、202）

```jsonc
// リクエスト
{
  "document_ids": ["doc_…"],          // または doc_type。どちらか一方（両方・どちらも無し → E1003）
  "doc_type": "invoice",
  "statuses": ["uploaded", "needs_review", "failed"],  // doc_type とだけ。省略時はこの 3 つ
  "schema_id": null,                   // 省略時は帳票ごとに種別の最新版
  "supersede_review": false,           // 単体 /extract と同じ意味論
  "options": { "force_vl": false }
}
// 応答
{
  "accepted": [{ "document_id": "doc_a", "job_id": "job_…", "run_id": "run_…" }],
  "skipped":  [{ "document_id": "doc_b", "code": "E1005", "message": "確定済みの結果があります…", "reason": "confirmed" },
               { "document_id": "doc_l", "code": "E1005", "message": "他のユーザーが確認中です", "reason": "locked" },
               { "document_id": "doc_c", "code": "no_schema", "message": "種別「receipt」の定義（スキーマ）がありません", "reason": null },
               { "document_id": "doc_x", "code": "E1001", "message": "ドキュメントが見つかりません", "reason": null }],
  "truncated": false
}
```

- 帳票ごとの判定は単体と同一（`_start_extract`）: 不在 E1001 → schema_id の存在 →
  **確定済み拒否（D3）→ 確定処理中 in_review 拒否 → 他者ロック拒否（D9）**（ここまでは
  `supersede_review` に依らない）→ `supersede_review` なら processing 競合・needs_review を
  superseded に落とす／既定なら processing + needs_review を競合 → run/job 作成 →
  `queued` → enqueue。
- E1005 の `reason`: `confirmed` / `in_review` / `locked` / `processing`（supersede 時の
  処理中）/ `active_run`（既定時の processing + needs_review）。web の要約（`classifySkip`）は
  これで数え、文言には依存しない。単体 `/extract` では同じ値が `error.details.reason` に入る。
- リクエスト全体のエラー（E1003 形の誤り・上限超過、E1001 存在しない `schema_id`）は
  **1 件も触らずに** 4xx。
- `document_ids` の重複は 1 回に数える。
- `Idempotency-Key` は単体と同じキャッシュ（名前空間 `extract-batch:` で分ける。同じキーを
  単体→一括で使い回されても応答の形が混ざらない）。
- 応答は全件 skipped でも 202。何が起きたかは本文が伝える。

## 3. UI

### 一覧（SCR-02）のアップロード

- file input は `multiple`、D&D も全ファイルを受ける。取り込める種類（PDF / PNG / JPEG /
  TIFF）以外は**投げる前に**除外して件数を伝える（`partitionFiles`）。
- **逐次**アップロードする（同時に投げると ingest の前処理が並列に走って遅くなる／
  順序が読めなくなる）。進行は「3/10 件をアップロード中…」の 1 行。
- 終わったら 1 つの要約トースト（成功 N 件・失敗 M 件・抽出投入 K 件）。失敗と抽出開始
  失敗には**サーバの理由**を添える（E1002 サイズ上限・E1001 非対応形式・403 など。
  同じ文言はまとめ、3 種類まで＋「ほか」。`summarizeReasons`）。件数だけだと同じ
  ファイルを何度も投げ直すことになる。
  **ちょうど 1 件**のアップロードのときだけ従来どおり帳票ページへ遷移する。複数なら
  一覧に残る（次の操作は一覧で行うため）。
- 「アップロード後に抽出を開始」チェックボックス: 種別を選んだときだけ有効、既定 on。
  on なら 1 通ごとに `extract-batch` を `document_ids: [その帳票]`・`schema_id` 省略で
  呼び、使う版はサーバが帳票の種別の最新版を解く（D6）。`GET /doc-types` の一覧は
  セレクトのラベルにだけ使う。開始できなかった帳票（skipped・通信エラー）は理由付きで
  要約トーストに数える（アップロード自体は成功している）。
- 1 件アップロードで抽出を開始した直後に遷移する帳票ページは、run が `processing` の
  間は結果が空に見える。検証画面（SCR-03）は `processing` の間だけ 3 秒ごとに結果を
  取り直し、終われば止める（一括再抽出の対象を開いたときも同じ）。

### 一覧の選択と「選択した N 件を再抽出」

- 各行に選択チェックボックス、ヘッダに「全選択」（**表示中の行**だけ）。行クリックの
  遷移とは伝播を切る。
- ツールバーの「選択した N 件を再抽出」→ 確認ダイアログ:
  - 種別のある件数と無い件数（無い分はスキップされると明示）
  - 「レビュー待ちの結果も置き換える（supersede）」既定 on
  - 確定済みは置き換えない、確定処理中と他の利用者が確認中の帳票もスキップされると注記（D3 / D9）
- `extract-batch` を `document_ids` で呼び、要約トースト
  「N 件を再抽出に投入しました（M 件はスキップ: 確定済み 2 / 処理中 1 / 他の利用者が確認中 1 / スキーマなし 1）」
  （`summarizeBatch`。内訳は skipped の `reason` で数える）。投入後は選択を解除する。
- queued / processing の帳票が一覧にある間は 5 秒ごとに再取得（D8）。

### スキーマ保存後のトースト（`useSchemaSaved`）

- 既存の「この帳票を再抽出」に加えて「この種別の帳票をすべて再抽出」を出す。
  `extract-batch` を `doc_type` + `supersede_review: true` で呼び、同じ要約トーストを出す。
  母集合は既定（uploaded / needs_review / failed、新しい順に 200 件）。確定済みは入らない。
  他の利用者が確認中の帳票はサーバが skipped にする（D9）ので、確認文言でもそう伝える。
- 押す前に確認する（件数はサーバが決めるので「最大 200 件」と伝える）。
- 保存後は `["schemas"]` に加えて一覧の種別セレクト `["doc-types"]` も invalidate する
  （スキーマ管理画面の保存も同じ）。作成した種別が 5 分間セレクトに出ない、を避ける。

### 権限

uploader がアップロード・抽出・一括投入まで行える（サーバも uploader）。削除の
reviewer 以上・テンプレート化の admin は従来どおり。

## 4. やらないこと

- **アップロードの並列化・再開**: 逐次で足りる規模（数十件）を対象にする。数百件は
  フォルダ監視（gdrive 接続）へ。
- **一括の進捗画面**: 一覧の状態列と 5 秒再取得で代える。ジョブ単位の完了通知は 1 通用
  （`useExtractJob`）のまま。
- **確定済みの一括取り直し**: D3。必要なら 1 通ずつ確定を解く運用（現状その API も無い）。
- **スキーマレスの一括抽出**（種別なしの帳票をまとめて自動発見）: D5。自動発見は
  「値を見てからテンプレート化する」1 通目の工程であり、まとめて回す理由が無い。
