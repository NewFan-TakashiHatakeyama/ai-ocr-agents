<!-- 生成: 2026-08-28 設計ワークフロー（調査4→起案→批評3→統合）。
     ユーザー要求: テンプレート化のプレビュー画面で読取領域/除外領域をドラッグ指定。
     関連: ADR-0006, DD-01。実装着手前のレビュー対象。
     更新: 2026-09-06 第2回敵対的レビュー反映済み（C1〜C35）。
     反映方針は末尾「付録: 第2回レビュー対応表」を参照。各節の変更は「（敵対的レビュー第2回 C#）」で追跡可能。 -->

# 詳細設計書（最終版）: 領域指定テンプレート化（region hint / exclude regions）+ F-0 フィールド BBOX 修正

対象リポジトリ: `C:/Users/takas/ai-ocr-agents`
関連 ADR/DD: ADR-0006（テンプレートレス・ファースト）, DD-01（前処理後 PNG が座標系の正）, DD-02, DD-09
本書は草案に対する 3 批評（product / tech / scope）の指摘を全て取り込み済み。各指摘の対応は末尾「付録: 批評対応表」で個別に消し込む。
さらに**第 2 回敵対的レビュー（C1〜C35）を反映済み**。第 2 回の対応は末尾「付録: 第2回レビュー対応表」で個別に消し込む。

---

## 1. 背景と目的

**ユーザーの課題**: 現状の抽出は AI が項目を自動発見するのみで、(a) 帳票タイトルなど「ユーザーが読みたい項目・領域」を UI から指定できず、(b) 印影・会社ロゴ・DB 管理しない情報が抽出結果に混入し、(c) 検証画面でテキストフィールドに BBOX が出ない（既知欠陥 F-0）。

**この設計が解くこと**:
1. テンプレート化ボタン → 全画面プレビュー → 「この抽出結果をテンプレート化」の導線で、読み取りたい領域（include）と読み取りたくない領域（exclude）をドラッグで設定できる。
2. include 領域は **hint**（プロンプト誘導 + 決定論の位置ガード）であり hard crop ではない。exclude 領域は「DB に載せない」が目的なので**決定論フィルタ**だが、削除の観測性（metrics / ReviewItem / 検証画面オーバーレイ）を必ず伴う。
3. F-0 を kie.py の 1 箇所で修正し、テキストフィールドにも BBOX を表示する。
4. **テンプレートレスという製品コンセプトを壊さない**ことを最優先制約とする。具体的には「ユーザーが明示的に確定した領域だけが保存される（オプトイン）」「矩形を一切触らずに保存した場合、生成されるスキーマは現行機能と完全に同一」「領域は値を捨てる根拠にならない」「作成後に領域を編集できる」の 4 点を受け入れ条件に含める。

---

## 2. 決定事項サマリ

| # | 決定 |
|---|---|
| D1 | 領域の意味論: include は hint（値を捨てない）。exclude は決定論フィルタ（span/セル）だが、削除件数を run metrics に記録し、セル/行削除が発生した run は ReviewItem を積み、検証画面に読み取り専用オーバーレイを出す。 |
| D2 | **発見済みフィールドの bbox は破線ゴースト（参考表示・保存対象外）**。ユーザーがゴーストをクリックして確定するか、自分で描いた矩形だけが region として保存される。**矩形を一切触らず保存 = 現行と同一のスキーマ**（受け入れ条件）。 |
| D3 | スキーマ保存座標は正規化 [0,1] float の `rect`。ランタイムの画素 `bbox`（int）とキー名で区別。Span/ExtractedField/TableCell の bbox 契約（前処理後 PNG 画素 int）は一切変えない。 |
| D4 | `RegionRect` は `packages/schemas/src/newfan_schemas/field_schema.py` に**一元定義**し、gateway はそれを import する（3 重定義しない）。 |
| D5 | `page` 指定は `int（1始まり） | "last" | null`。`"last"` はページ数可変帳票の最終ページ（承認印・合計欄）用。`null`（全ページ）は exclude のみ許可。 |
| D6 | exclude は `field_schemas.exclude_regions` 新列（migration 0007）。同時に `source_page_count INT NULL` 列を追加（テンプレート化時のページ数を記録、位置ガードの page 判定に使用）。 |
| D7 | `PutSchemaRequest.exclude_regions` / `source_page_count` は **`None` = 直前版から引き継ぎ / 明示 `[]` = クリア**。引き継ぎは `put_schema`（Pg/InMemory 両実装）の内部で行うため、旧編集画面・chat 経路は**無変更のまま安全**。 |
| D8 | exclude の適用点はグラフノードを増やさず `structure_ocr` / `vl_fallback` ノード内部の純関数フィルタ。`mask_tables` は**セルを削除せず value/text/span 参照を空にする**（検証 UI の列ズレ防止）。マスク発動した TableResult は `structure_html` を None 化。 |
| D9 | `filter_blocks`（LayoutBlock フィルタ）は **v1 から削除**。checkpoint 衛生としては中途半端（被覆 0.5–0.9 のブロック content が残る・原本 PNG も残る）であり、ADR に保証範囲を正直に書く方を選ぶ。 |
| D10 | 位置ガードは confidence を触らない。**`confidence_gate_node` 内で判定**し（auto_elevate 巻き戻し・llm_correct 誤発火の両方を回避）、v1 は **shadow mode（metrics + ログのみ、ReviewItem を積まない）**で出荷。実測後にフラグで有効化。有効化時は doc レベル判定（region フィールドの過半が同時 mismatch → 別レイアウトと判定し per-field レビューを抑止）を伴う。 |
| D11 | KIE プロンプトへの region ヒント注入（region_px + spans への bbox 追加 + yaml 追記）は **Phase 4 に分離し、fixture での精度計測をゲート**にする。ただし `"region": null` によるプロンプト汚染防止（schema からの region キー除去）だけは **Phase 1 で必須**。 |
| D12 | プレビュー UI は既存ダイアログを全画面 `.tpl-overlay` に拡張（クラス名維持必須 — `web/app/documents/[id]/page.tsx:131-137` のショートカットガードが依存）。v1 の矩形操作は「描く・選ぶ・消す・ゴースト確定・モード切替」のみ。**移動・四隅リサイズハンドルは v1 から削除**（再ドラッグ置換で代替）。キャンバスは**ページ全体 fit（contain）表示**でスクロール問題を消す。ズームは非ゴール。 |
| D13 | **スキーマ適用済み帳票からの「領域・項目を編集」導線を Phase 3 必須スコープに含める**（write-once 禁止）。既存最新版をプリロードし、`create:false` で新版として保存（明示警告付き）。**新プレビューは意味属性（required / critical / columns）を編集しない**（編集は旧 `/schemas` 画面に残す）が、**プリロード元フィールドをスプレッド保全して往復で失わない**（敵対的レビュー第2回 C4）。 |
| D14 | `GET /v1/documents/{id}` に `pages: [{page_no, width, height}]` を追加する（未訪問ページの正規化に必須。草案 §8.10「pages 寸法 API を追加しない」は撤回）。**`DocumentMeta` は一覧 API と共用のため既定値は空配列で、埋めるのは単体取得のみ**（一覧の N+1 回避。敵対的レビュー第2回 C25）。 |
| D15 | 保存 UI に適用範囲を明示し（→ D17 により「手動抽出のみ / ワークフローは版固定」と正確に書く）、保存成功トーストに「この帳票を再抽出」ボタンを付ける。**「既存の抽出開始 API 再利用」は撤回**（敵対的レビュー第2回 C2/C3/C6/C11）: (a) `POST /v1/documents/{id}/extract` は `has_active_run`（`processing` **と `needs_review` の両方**を実行中とみなす。`routers.py:354` / `db.py:173-179`）で E1005(409) を返すため、テンプレート化の典型状態（自動発見 run が needs_review）では必ず失敗する。よって同 API に `supersede_review: bool = false` を追加し、true のときだけ競合判定を `has_processing_run` に切り替える（`chat_tools.rerun_extract` と同一意味論。既定 false で外部連携の二重投入防止は不変）。**このサーバ変更は Phase 2 までに入れる**（Phase 3 の UI だけ先行すると 409 トーストになる）。(b) 再抽出 body の `schema_id` には **PUT /v1/admin/schemas 応答の `SchemaDto.id`（新版 id）を明示送信**する（classify / ExtractStart の推定ロジックは流用しない。未指定はスキーマレス run になり region/exclude が無音 no-op になる）。(c) 確定済み（`confirmed` / `exported`）run を持つ帳票ではボタンを出さない（確定値の無警告置換防止）。ロック保持者が他者（`useDocumentLock` readOnly）のときも出さない。過去 run の一括遡及はやらない。 |
| D16 | リリース順序の強制: migration 先行 → Phase 1（保存契約）→ Phase 2（除外パイプライン）→ Phase 3（UI）。**Phase 2 と 3 の並行は禁止**（UI が先に出るとサイレント no-op になり信頼を壊す）。Phase 4（ヒント）は計測ゲート付きの独立判断。**Phase 1 内は gateway と orchestrator-worker の相互順序も規定する**（§4.7 / §9。敵対的レビュー第2回 C27/C29）。 |
| D17 | **ワークフロー `process.extract` は `schema_id`（版 id）固定であり、テンプレート化 / 編集モードで保存した新版は既存ワークフローに自動適用されない**（`put_schema` は常に新 uuid の新版 INSERT、`workflow_graph.py:133-138` は `node.config.schema_id` で解決）。v1 は「①保存成功トーストで旧版を参照する有効ワークフロー件数と導線を出す」「②lint に L012（warning）『extract.schema_id が当該 doc_type の最新版ではありません』を追加」の 2 点で**可視化**に留め、自動付け替え（repoint）は v2。§4.4b / §10-16 / §11-9（敵対的レビュー第2回 C5）。 |
| D18 | **exclude は doc_type（スキーマ版）単位の決定論削除**であり、同一 doc_type を共有する別レイアウト取引先の帳票でも同座標に適用される。v1 は保存 UI の明示警告・オーバーレイ・「除外 span > 0 かつ required/critical の grounding 喪失」集約 ReviewItem で**検知に倒す**。取引先/レイアウト単位のスコープ化は v2（§11-8。敵対的レビュー第2回 C19）。 |

---

## 3. UX 詳細

### 3.1 画面遷移

```
[作成モード]
帳票詳細 (web/app/documents/[id]/page.tsx)
  schema_id === null && createdDocType === null のバナー（既存条件のまま）
  └ 「テンプレート化」ボタン (web/components/TemplatizeSchema.tsx)
      → TemplatizePreview（全画面 .tpl-overlay、ルーティング変更なし）
      → 「この抽出結果をテンプレート化」 → PUT /v1/admin/schemas (create:true)
      → 成功トースト「作成しました。**手動抽出・分類推定には最新版が使われます**」
         + [この帳票を再抽出] ボタン（条件付き。下記「再抽出ボタンの仕様」）
         + 旧版を参照する有効ワークフローがあれば「ワークフロー N 件が旧版を参照しています」+ 各リンク（§4.4b / D17）
      → onCreated（既存の setCreatedDocType 導線。**schema id も併せて受け取る** — `onCreated({docType, schemaId})`）
      ※ 既存 doc_type と衝突した場合は従来どおり E1005(409) を表示（無警告上書きは発生しない
        — routers.py:728-733 で確認済み。批評 product-4 の「無警告で新版誕生」は create モードでは事実誤認）

[編集モード（新設・Phase 3 必須）]
出現条件（admin のみ。**page.tsx:290-293 の「=== null の厳密比較・undefined なら出さない側に倒す」方針と統一** — 敵対的レビュー第2回 C30）:
  typeof data.schema_id === "string"        // 旧 gateway 混在窓の undefined では出さない
  || createdSchemaId !== null               // 作成モードで保存した直後（run.schema_id は null のまま — page.tsx:68-71）
  → この 2 条件により「領域を描いた当の帳票では二度と編集できない」write-once 事故を防ぐ（敵対的レビュー第2回 C7）
  └ 「領域・項目を編集」ボタン（新設。バナー外・ヘッダ付近。createdDocType バナー内にも同ボタンを置く）
      → 同じ TemplatizePreview を編集モードで開く
        - **プリロードは doc_type 起点**（敵対的レビュー第2回 C9）:
          `list_schemas` は doc_type ごと最新版のみを返し、`run.schema_id` は抽出時点の**旧版 id であり得る**ため
          id 突合は不可（`db.py:633` / `put_schema` `db.py:678`）。よって
          (1) `ResultResponse` に `schema_doc_type: Optional[str]` を追加（サーバ側で `admin.get_schema_by_id(run.schema_id)`
              から解決。§6 の `applied_exclude_regions` と同じ 1 SELECT で取れる）、
          (2) web は `api.getSchema(docType)`（既存 `GET /v1/admin/schemas/{doc_type}` = 最新版、`routers.py:707`）で取得。
          編集対象 doc_type の決定順は `schema_doc_type` → `createdDocType`。
          `schema_id` は「vN → v(N+1)」の版表示と「この帳票は旧版 vK で抽出済み」注記にのみ使う。
          取得失敗（doc_type が null / スキーマ削除済み）の場合は編集ボタンを**無効化**し、空プリロードでの保存を禁止する。
        - fields / region / exclude_regions をプリロードし、**元フィールドを `base` として丸ごと保持**（§3.3。
          required / critical / columns と未知キーを新版で失わない — 敵対的レビュー第2回 C4）
        - この帳票の抽出結果 bbox はゴースト（参考表示）として重畳
      → 保存時に「スキーマ vN の新しい版 v(N+1) として保存します」を明示 → PUT (create:false)
```

**再抽出ボタンの仕様（D15 の実装規定・敵対的レビュー第2回 C2/C3/C6/C11）**

| 項目 | 仕様 |
|---|---|
| 出し分け | `data.status`（run status）が `confirmed` / `exported` のときは**表示しない**（確定値＝会計連携済みが最新 run に覆われるため）。`useDocumentLock` が readOnly（他者がロック保持）のときも表示しない。それ以外（`needs_review` / `failed`）で表示。 |
| 経路 | `POST /v1/documents/{id}/extract` に `supersede_review: true` を付けて呼ぶ。サーバ側は同フラグ時のみ競合判定を `has_active_run` → `has_processing_run` に切り替える（`chat_tools.rerun_extract` と同一意味論）。**このサーバ変更は Phase 2 までに入れる**（D15(a)）。 |
| schema_id | PUT /v1/admin/schemas 応答の `SchemaDto.id`（新版 id）を `body.schema_id` に**明示送信**。classify 推定・`ExtractStart` の既定選択は流用しない（未指定＝スキーマレス run になり region/exclude が完全 no-op — D16 が最も恐れる「保存したのにサイレント no-op」がボタン自身で起きる）。 |
| 旧 run の扱い | `supersede_review` で新 run を作る際、既存 `needs_review` run は `superseded` に遷移させる（`get_latest_run` / 削除ブロッカー / `workflow_graph.py:487` の hitl_gate が旧 run を見続けないようにする）。`GET /documents/{id}` は新 run を返す。 |
| 非同期処理 | トーストの `action` は同期 `onClick`（`Toaster.tsx:20` は戻り値を捨てるため未処理 rejection になる）なので、**ボタンは `void run()` の形で page 側 mutation を起動する**。ポーリングは `ExtractStart.pollJob` を `web/lib/useExtractJob.ts` へ切り出して共有し、成功時に `qc.invalidateQueries({queryKey:['result', id]})` と `['documents']` を実行。`processing` 競合の 409 は warn トースト「処理中です。完了をお待ちください」で表示する。 |
| 表示位置 | トーストはプレビューを閉じた**後**に出す（§3.4 の z-index 規定と対。敵対的レビュー第2回 C17）。 |

### 3.2 コンポーネント構成

```
web/components/TemplatizeSchema.tsx（既存: ボタン + open 状態のみ残す）
└ web/components/TemplatizePreview.tsx（新規。全画面 .tpl-overlay。local state で完結、zustand 不使用）
   ├ web/lib/usePageImage.ts（新規フック。DocViewer.tsx の署名 URL 取得ロジックを切り出し共有）
   ├ web/components/RegionCanvas.tsx（新規。ページ全体 fit 表示 + 矩形描画）
   └ 右ペイン: doc_type 入力（編集モードでは読み取り専用）+ Draft 行テーブル + 除外領域リスト + 保存
        + インライン検証領域（role="alert"。下記）
```

**プレビュー内メッセージはトーストを使わない（敵対的レビュー第2回 C17）**: `.toast-wrap` は `z-index:50`（`globals.css:946-953`）、`.tpl-overlay` は `z-index:100`（同 :1902-1910）であり、プレビュー表示中に `push({kind:'warn'})` したトーストは幕の下に沈んで × も action も押せない（`toast.ts:62` により warn は自動消去もされない）。よって:
- doc_type 空 / 未紐付け include / 重複 name / 行未選択 / 403 / 409 等の検証・API エラーは、**右ペインの保存ボタン近傍のインライン領域（`role="alert"`）に表示**する。
- トーストは `setOpen(false)` 後の成功通知（D15 の再抽出ボタン付き）に限定する。
- 併せて Phase 3 の `globals.css` 変更に「`.toast-wrap` の `z-index` を `.tpl-overlay` より上（200）へ引き上げる」を含める（既存 `issue()` 警告の救済。二重防御）。

**右ペイン Draft 行の型表示（敵対的レビュー第2回 C4）**: `TYPE_OPTIONS`（`TemplatizeSchema.tsx:35-43`）に `table` は存在しない。編集モードで `type === "table"` の行が来たときに select が表示不能になるため、`table` を TYPE_OPTIONS に追加した上で**当該行の型 select は読み取り専用**にする（`columns` はそのまま往復させ、削除・改変しない）。table 行は include 領域の対象外でよいが、**保存対象からは外さない**。

### 3.3 状態一覧

```ts
type DraftRow = {
  rowId: string;          // crypto.randomUUID()。region との紐付けは name でなくこの id（rename 耐性）
  name: string; label: string; type: string; include: boolean;
  base?: SchemaFieldDto;  // 編集モードでプリロードした元フィールド丸ごと（敵対的レビュー第2回 C4）
};

type PreviewRegion = {
  id: string;
  page: number | "last";
  bbox: [number, number, number, number];  // 画像 px（前処理後 PNG 座標）。保存時にのみ正規化
  kind: "include" | "exclude";
  rowId?: string;          // include のみ。DraftRow.rowId
  label?: string;          // exclude のみ（"印影" 等、任意）
  allPages?: boolean;      // exclude のみ（page: null 化）
};

// state:
//   drafts: DraftRow[]
//   regions: PreviewRegion[]           // 確定済み領域のみ。ゴーストは含まない
//   selection: { rowId: string } | { regionId: string } | null
//     ※ include 領域と Draft 行は 1:1 なので選択は単一エンティティに統合する。
//       「矩形選択→行ハイライト」「行選択→矩形ハイライト」は同一 selection の派生表示であり、
//       双方向同期機構は存在しない（批評 D-11 の同期リスクをこの構造で吸収）
//   mode: "include" | "exclude"
//   currentPage: number
//   pageDims: Map<number, {width, height}>  // GET /documents/{id} の pages から（§6）
```

編集中は画像 px で保持し、保存時にのみ `pageDims`（サーバ返却の正規寸法）で割って正規化する。初期表示（編集モードのプリロード）は逆変換。未訪問ページの領域も `pageDims` があるため正規化可能（naturalWidth 依存を廃止 — 批評 tech-4）。

**保存時 fields の生成規則（属性保全・敵対的レビュー第2回 C4）**:

```ts
// 編集モード: base があれば未知キーを含めて丸ごと引き継ぐ
fields = drafts.filter(d => d.include || d.base)     // base 持ちは include=false でも版から落とさない（table 行等）
  .map(d => d.base
    ? { ...d.base, name: d.name, label: d.label, type: d.type, region: regionOf(d) }
    : { name: d.name, label: d.label, type: d.type, required: false, critical: false, region: regionOf(d) });
```

現行 `TemplatizeSchema.save`（`TemplatizeSchema.tsx:110-118`）は `required:false / critical:false` を固定し `columns` を送らない。編集モードがこの形のまま `create:false` で PUT すると、`put_schema` は常に全置換の新版 INSERT であるため、旧編集画面で設定した `required` / `critical` と table 型の `columns` が新版で**全滅する**。`base` スプレッドはこれを構造的に防ぐ（§4.2 の「旧画面はスプレッド複製で未知キーごと往復保全」と同じ原則を新プレビューにも適用）。**作成モードは現行どおり `required:false` / `critical:false` / `columns` 無し**。

**不変条件（敵対的レビュー第2回 C32）**: `regions` の include 領域は必ず `include=true` の DraftRow に紐付く。保存時の `filter(d => d.include)` は**防御的二重化**であり、通常経路では発動しない（UI 側で include=false 行に確定済み領域が残る状態を作らない。§3.4「ゴースト確定」「include 解除」）。

### 3.4 矩形操作仕様（v1）

| 操作 | 仕様 |
|---|---|
| ゴースト表示 | bbox を持つ発見済みフィールドを破線・半透明で表示（`.bx-ghost`）。**保存対象外**。 |
| ゴースト確定 | ゴーストをクリック → 該当 Draft 行に紐付く include 領域として確定。確定時に**自動パディング**: 各辺 `max(round(min(W,H) * 0.02), 12px)` 外側へ拡張（ページ境界で clamp）。タイトな外接矩形をそのまま保存するとスキャン分散で誤 mismatch が出るため（批評 product-7）。**対象 Draft 行が `include=false` の場合は確定と同時に `include=true` へ切り替える**（領域指定＝抽出したいの意思表示。敵対的レビュー第2回 C32）。 |
| include 解除 | 確定済み領域を持つ行のチェックを外したら、その include 領域も**同時に削除**する（再チェックしても復活しない）。値が取れなかった項目は初期値 `include=false`（`TemplatizeSchema.tsx:73`）なので、この規則が無いと「矩形は見えているのに保存後に無音で消える」状態分岐漏れが発生する（敵対的レビュー第2回 C32）。 |
| 描く | 空所で pointerdown → `setPointerCapture` → move で進行中矩形表示 → up で確定。毎イベント `getBoundingClientRect()` 基準。表示 8px 未満はクリック扱いで破棄。逆ドラッグは min/max 吸収。kind は現在のモードトグルに従う。include モードで行未選択の場合は確定時に行選択を促す（未紐付け include は保存不可）。ユーザーが描いた矩形はパディングなしでそのまま。 |
| 選ぶ | 矩形クリックで selection 設定（排他）。include は対応 Draft 行もハイライト（同一 selection の派生）。 |
| 置換 | 既に領域を持つ行を選択した状態で描く → 置換（確認なし。再ドラッグで回復可能）。 |
| 消す | 選択中に Delete/Backspace、または矩形上の × ボタン。**`ev.target instanceof HTMLInputElement || HTMLTextAreaElement` なら無視**（page.tsx:137 と同型の typing ガード。批評 D-12）。Esc は選択解除のみ（ダイアログは閉じない）。 |
| 移動・リサイズ | **v1 なし**。位置修正は削除→描き直し or 置換（批評 D-11、工数 3〜4 割減）。 |
| モード切替 | 右ペイン上部トグル。`.bbox` 基底 + `.bx-include`（実線）/ `.bx-exclude`(斜線背景 + 別色) / `.bx-ghost`（破線）。 |
| exclude 行 | リスト行に label 入力（任意）と適用範囲セレクト:「このページ / 全ページ / 最終ページ」（→ page: N / null / "last"）。 |
| ページタブ | `GET /documents/{id}` の `page_count` から 1..N の全タブ（発見フィールドの無いページにも印影を描けるようにする）。`.pagetab` CSS 流用。 |
| 表示 | キャンバスは**ページ全体 fit（高さ contain）**。スクロール中ドラッグ問題を構造的に消す（批評 D-13）。img に ResizeObserver で scale 追随。ステージに `touch-action: none`。**ステージ内 `<img>` は `draggable={false}`**、ステージの `pointerdown` 先頭で `ev.preventDefault()`、`onDragStart` でも `preventDefault()`（ネイティブ画像ドラッグが始まると pointer capture 中でも `pointercancel` が発火し、進行中矩形が消えて up が来ない）。`.viewer-stage`/RegionCanvas に `user-select: none; -webkit-user-drag: none;` を付与（敵対的レビュー第2回 C31）。 |
| ページ切替中の描画禁止 | `usePageImage` は**ページ変更時に url を即 null へ戻し**（現行 `DocViewer.tsx:28-42` は旧画像を表示し続けるので、この挙動は切り出し時に継承しない）、ローディング表示に切り替える。`RegionCanvas` は「表示中 img の `onLoad` 完了済み **かつ** その img が `currentPage` のもの（`naturalWidth/Height` が `pageDims[currentPage]` と一致）」のときだけ `pointerdown` を受け付け、それ以外は `pointer-events: none`。ドラッグ中にページ切替が起きたら進行中矩形を破棄。`scale` は当該ページ img の `onLoad` と ResizeObserver の両方で `img.clientWidth / pageDims[currentPage].width` から再計算し、旧ページ値を引き継がない（縦横比の違うページで矩形が誤ページ・誤座標で保存される経路を構造的に閉じる。敵対的レビュー第2回 C18）。 |
| 署名 URL | ページ切替ごとに取得（現行方式）。403/期限切れ時は 1 回だけ自動再取得。 |
| 保存前警告（exclude） | 除外領域が 1 件以上ある状態の保存ボタン近傍に常時表示: 「**除外領域は同じ doc_type の全帳票に適用されます。レイアウトが異なる取引先の帳票では、その位置にある実データも取り込まれません。** 領域は対象（印影等）の外接より大きくしすぎないでください」（D18 / §11-8。敵対的レビュー第2回 C19）。include 領域が 1 件も無いスキーマで exclude だけを保存しようとした場合は追加で「読取領域が 1 つも無いとレイアウト違いを検知できません。少なくとも 1 項目の読取領域を確定してください」を表示する（**422 にはしない**）。 |

**受け入れ条件（オンボーディング摩擦防止・批評 product-1 / P-OD4）**:
- 矩形を一切操作せず doc_type と項目だけ確認して保存した場合、生成されるスキーマは現行 TemplatizeSchema と完全同一（region 無し・exclude_regions は作成モードで `[]`）。手数も現行ダイアログと同等（プレビューを開く→保存の 2 クリック増以内）。
- `.tpl-overlay` クラスは新プレビューでも必ず維持（Ctrl/⌘+Enter=会計連携発火のガードが `document.querySelector(".tpl-overlay")` に依存。ガード側は触らない）。
- **編集モードの属性保全（敵対的レビュー第2回 C4）**: 編集モードで矩形のみ追加して保存した場合、v(N+1) の fields は `region` 以外 vN と**完全一致**する（`required` / `critical` / `columns` および未知キーを含む）。矩形も触らず保存した場合は `region` を含めて完全一致する。
- **テンプレート化元の帳票からも編集できる（敵対的レビュー第2回 C7 / C30）**: 作成モードで保存した直後、および**ページをリロードした後**も、同一帳票から編集モードに入って v2 を保存できる（§1 受け入れ条件 4「作成後に領域を編集できる」の実効化）。
- **needs_review 状態からの再抽出が 409 にならない**（`supersede_review: true`）かつ**再抽出 run の `schema_id` が新版を指す**（敵対的レビュー第2回 C2/C6/C11）。

### 3.5 検証画面（DocViewer）への読み取り専用追加（Phase 3）

`web/components/DocViewer.tsx` に**表示専用**の 2 点を追加する（編集機能は追加しない — 草案 §3.3 の原則は維持。批評 product-3/9）:
1. run に適用された exclude 領域を `.bx-exclude-view`（薄い斜線）で重畳表示し、ホバーで「除外設定により未取込」を表示。座標は ResultResponse の `applied_exclude_regions` × naturalWidth/naturalHeight。**`applied_exclude_regions` はサーバ側でページ解決済み**（`{page_no: int, rect, label?}`）で返るため、DocViewer は既存の `(f.page ?? 1) === pageNo` と同型の `r.page_no === pageNo` でフィルタするだけでよい（`"last"` / `null` の解決を web に再実装しない。§6 / 敵対的レビュー第2回 C16）。
2. ヘッダに除外バッジ:「除外領域: span N 件 / セル M 件を未取込」（ResultResponse の `region_stats` から。0 件なら非表示）。
   **`region_stats` は `needs_review` 保存時点で載っている必要がある**（§5.4 の save_result 5 点目 = `worker.py` の interrupt 経路。ここが漏れるとレビュー中だけバッジが出ない — 敵対的レビュー第2回 C1/C13/C20/C21）。なお 1.（オーバーレイ）は schema 由来の `applied_exclude_regions`、2.（バッジ）は run metrics 由来で**依存経路が異なる**点に注意する。

---

## 4. データモデル

### 4.1 RegionRect（一元定義）

`packages/schemas/src/newfan_schemas/field_schema.py` に定義し、gateway は import する（批評 tech-16。「3 モデル同一コミット」制約は消滅）:

```python
class RegionRect(BaseModel):
    # 1始まり int / "last"（最終ページ・ページ数可変帳票用）/ None（全ページ。exclude のみ許可）
    page: Optional[Union[int, Literal["last"]]] = None
    rect: list[float]                 # [x1, y1, x2, y2] 正規化 0..1（当該ページ寸法比）
    label: Optional[str] = None       # exclude の表示名。include では未使用
```

validator: `len(rect)==4`、各値 `0<=v<=1`、`x1<x2`、`y1<y2`、面積 > 0.0001、page が int なら `>=1`。
コンテキスト依存の制約（include は page 必須 / exclude のみ None 可）は gateway `routers.py` の put_schema で検査し 422。

JSON 例:

```json
{
  "doc_type": "invoice_acme",
  "fields": [
    {"name": "title", "label": "帳票タイトル", "type": "string", "required": false, "critical": false,
     "region": {"page": 1, "rect": [0.30, 0.02, 0.72, 0.09]}},
    {"name": "total", "label": "合計", "type": "money", "required": true, "critical": true,
     "region": {"page": "last", "rect": [0.65, 0.80, 0.95, 0.88]}}
  ],
  "exclude_regions": [
    {"page": null,   "rect": [0.82, 0.02, 0.98, 0.14], "label": "社印"},
    {"page": "last", "rect": [0.05, 0.90, 0.30, 0.98], "label": "承認印"}
  ],
  "source_page_count": 2
}
```

### 4.2 フィールド別 region（fields JSONB 内・migration 不要）

- `packages/schemas/src/newfan_schemas/field_schema.py` `FieldDef` に `region: Optional[RegionRect] = None`
- `services/gateway/src/newfan_gateway/records.py:88` `SchemaFieldDef` に同フィールド（RegionRect を import）
- `services/gateway/src/newfan_gateway/dto.py:144` `SchemaFieldDto` に同フィールド。**records と同一コミット必須** — pydantic v2 既定 `extra="ignore"` のため、DTO を忘れると GET /schemas 応答から region が消え、旧編集画面の「新版として保存」往復で region が全滅する。
- chat 経由スキーマ編集（`routers.py` update_schema / `chat_tools.py`）は SchemaFieldDef を並べ直して put するため、モデル変更のみで region 保全。旧編集画面 `web/app/(admin)/schemas` 系はフィールドをスプレッド複製するため、サーバが region を返せば未知キーごと往復保全（確認済み）。

### 4.3 field_schemas 新列（migration 0007）

`db/migrations/versions/0007_schema_regions.py`（0006 と同じ「ADD COLUMN IF NOT EXISTS + DEFAULT → migrate 先行で安全」パターン）:

```python
op.execute("ALTER TABLE field_schemas ADD COLUMN IF NOT EXISTS exclude_regions JSONB NOT NULL DEFAULT '[]'")
op.execute("ALTER TABLE field_schemas ADD COLUMN IF NOT EXISTS source_page_count INT")
```

- `exclude_regions`: RegionRect の配列。
- `source_page_count`: テンプレート化時の帳票ページ数（nullable）。位置ガードの「run のページ数が違うなら page 不一致を問わない」判定に使う（批評 product-6）。

**fields JSONB 内に exclude を置かない理由（維持）**: bare 配列を全読者が舐める前提。擬似フィールドは classify の分類語彙を汚染し編集画面にも並ぶ。

### 4.4 引き継ぎセマンティクス（データ破壊防止・最重要）

`put_schema` は常に新版 INSERT（`db.py:678-702`）であり、旧編集画面（`web/lib/api.ts:155-159` — body は `{doc_type, fields, create}` のみ）と chat 経路（`routers.py` update_schema / `chat_tools.py`）は exclude_regions を送らない。「省略時 `[]`」にすると**旧経路の保存 1 回で除外設定が全滅する**（批評 tech-1 / scope A-1）。よって:

- `PutSchemaRequest.exclude_regions: Optional[list[RegionRect]] = None`、`source_page_count: Optional[int] = None`
- `admin.py` Protocol: `put_schema(tenant_id, doc_type, fields, exclude_regions=None, source_page_count=None)`
- Pg 実装（`db.py` put_schema）: 引数が `None` のとき、当該 doc_type の現行最新版から `exclude_regions` / `source_page_count` を SELECT してコピーし INSERT。明示 `[]` のみクリア。InMemory 実装（`admin.py:126`）も同セマンティクス。
- **引き継ぎ用 SELECT の実装制約（敵対的レビュー第2回 C22）**: 版採番と**同一トランザクション内**で `WHERE tenant_id=:t AND doc_type=:d ORDER BY version DESC LIMIT 1`（`get_schema` と同一の版選択）により行う。`ORDER BY` を落とすと v1 の設定が復活して v2 以降の設定が消える、という InMemory では検出できない事故になる。
- **戻り値も引き継ぎ後の実値にする（敵対的レビュー第2回 C28）**: 現行 `db.py:702` / `admin.py:126` は `SchemaRecord(..., fields=list(fields))` を**引数から**組み立てて返す。引き継ぎ（None）時は戻り値に **INSERT した確定値**（前版からコピーした `exclude_regions` / `source_page_count`）を載せ、**PUT 応答 = 直後の GET 応答**とする。旧編集画面は PUT 応答で画面 state を差し替えるため、応答が `[]` だと「除外設定が消えた」ように見え、次の保存で明示 `[]`（＝本当にクリア）を送る誘発経路になる。
- **引き継ぎは Pg の SQL に依存するため、InMemory ユニット（§8 `test_legacy_put_without_exclude_key_inherits`）だけでは担保されない**。実 Pg での多版シナリオテストを **Phase 1 の完了条件**に含める（§8 統合 / §9。敵対的レビュー第2回 C22）。
- この設計により **chat 2 経路・旧編集画面はコード無変更で安全**。

### 4.4b ワークフローとの版固定関係（敵対的レビュー第2回 C5 / D17）

`put_schema` は常に**新 uuid の新版 INSERT**（`db.py:678-702`）である一方、ワークフロー（⑤⑥ 自動取込＝本番の主経路）の抽出ノードは `field_schemas.id` を固定保持する（`workflow_graph.py:133-138` `_make_extract` が `node.config.schema_id` で `ensure_extract_run` → `pg_persistence.py:39-42` が `WHERE id = :s`）。したがって:

- **テンプレート化 / 編集モードで保存した v2・v3 は、既存ワークフローに pin された v1 の id からは決して引かれない。** 運用者は「保存したのに印影が消えない／領域を直したのに効かない」を無音で踏む（`lint.py:204` の L009 は「存在するか」しか見ないので警告も出ない）。これは region に固有の問題ではなく、既存の項目編集と同じ性質である。
- **v1 の対処（可視化のみ）**:
  1. **保存成功トーストの警告**: 当該 doc_type の旧版 id を extract ノードに持つ active ワークフローを `listWorkflows` の `graph_json` 走査（web 側で可能・新 API 不要）で検出し、「ワークフロー N 件が旧版を参照しています」+ 各ワークフローへのリンクを出す。文言は「手動抽出・分類推定には最新版が使われます。有効化済みワークフローは版 ID 固定のため、extract ノードのスキーマを選び直して再有効化してください」。
  2. **lint L012（warning）**: 「`extract.schema_id` が当該 doc_type の最新版ではありません（v1 / 最新 v3）」。`schema_exists` と同型の注入関数 `schema_is_latest` を gateway から渡す（`routers.py:901` の呼び出しに 1 引数追加）。**error ではなく warning**（既存ワークフローの有効化を塞がない）。
- **v2 候補（本件スコープ外・§10-16）**: (a) `put_schema` 成功後に同テナント・同 doc_type の旧版を参照する `workflows` の extract ノードを新版へ自動付け替え（`workflows_repo.repoint_schema(tenant, doc_type, old_ids, new_id)`、active/draft 双方）、(b) `ExtractConfig` に `doc_type` を追加し実行時に最新版を解決（`schema_id` と排他）。いずれも「有効化済みワークフローの挙動が管理者の知らないところで変わる」副作用があるため、実測とレビューを経て別チケットで判断する。

### 4.5 SELECT の明示列挙（本番だけ壊れる事故の再発防止）

`db.py` の `get_schema_by_id` / `list_schemas` / `get_schema`（:660-676 ほか）の SELECT に `exclude_regions, source_page_count` を**列追加後すぐ**追加。`SchemaRecord`（records.py:97）に `exclude_regions: list[RegionRect] = []` / `source_page_count: Optional[int] = None`。DDL 整合プローブ（`services/gateway/tests/test_pg_repository_integration.py`）の対象に載せる。

### 4.6 orchestrator 側の置き場（一本化・批評 scope B-4）

exclude_regions **および `source_page_count`** は **`LoadedContext` の独立フィールドと ExtractionState のトップレベルキーのみ**に置く。`EMPTY_SCHEMA`（`pg_persistence.py:20`）と schema dict には**入れない**（`make_kie_extract` が schema を `json.dumps` でプロンプトに埋めるため、座標やページ数がプロンプトを汚染する）。草案 §2.3 の「EMPTY_SCHEMA に exclude_regions を追加」は**撤回**。

**`source_page_count` の配線を明記する（敵対的レビュー第2回 C10 / C14）**: §5.5 の位置ガード手順 2 は「run の page_count が `source_page_count` と異なる場合は page を問わない」と規定しているが、`confidence_gate_node`（`nodes.py:214`）は state しか見られない。exclude_regions と**同じ 4 点**を通さないとこの判定は実装不能で、ページ数可変帳票（product-6 対応）で毎回 mismatch を記録し、Phase 5 の許容パラメータ実測（§11-1）を汚染する。

- `services/orchestrator/src/newfan_orchestrator/persistence.py:17` `LoadedContext` に `exclude_regions: list = field(default_factory=list)` と **`source_page_count: Optional[int] = None`**。`InMemoryContextStore.seed_run` にも同 2 引数。
- `pg_persistence.py` `load_context`: field_schemas の SELECT を **`doc_type, fields, exclude_regions, source_page_count`** にし、`LoadedContext.exclude_regions` / `LoadedContext.source_page_count` へ（**schema dict には入れない**）。
- `db_nodes.py:26` の return に `"exclude_regions": ctx.exclude_regions` と **`"source_page_count": ctx.source_page_count`** を追加。
- `packages/schemas/src/newfan_schemas/extraction.py:79` `ExtractionState` に `exclude_regions: list[dict[str, Any]]` と **`source_page_count: Optional[int]`** を追加（TypedDict total=False、追記のみ）。

**ページ寸法の配線（追加不要・ただし縮退規則が必須。敵対的レビュー第2回 C26 / C34）**: ページ寸法は既に `LoadedContext.pages`（`pg_persistence.py:45-54` の SELECT が `width` / `height` を返す）に載り `db_nodes` 経由で `ExtractionState["pages"]` に入る。**追加配線は不要**。ただし:

- `pages.width` / `height` は **NULL 許容**（`0001_initial_schema.py:92`。本番 ingest は必ず埋めるが `workflow_store.py:753` の INSERT は `p.get("width")` で NULL を書き得る）、かつ **InMemory テストの seed は寸法を持たない**（`test_worker.py:70-76`, `test_ocr_nodes.py` は `{"page_no", "image_uri"}` のみ）。素直に `page["width"]` と書くと exclude が空でも既存 test_worker が全件 KeyError で落ち、`page.get("width")` にすると exclude 有り run でのみ `None * float` の TypeError になる。`ocr_nodes.py` の try/except はページ読込と layout_parsing（:137-154）しか覆っていないため、例外はノード全体を落とし worker は ACK せず**再配信ループ**に入る。
- したがって規約: **`w, h = page.get("width"), page.get("height")`。いずれかが None / 0 以下の場合、当該ページの exclude 適用と位置ガードは no-op（`px_regions = []`・mismatch 判定もしない）とし、run は落とさない。** 同時に `logger.warning("[structure_ocr] page dims missing; exclude regions skipped page=%s")` と `metrics["region"]["skipped_pages_no_dims"]`（page_no の配列）に記録して**観測可能にする**（§5.4「決定論削除には必ずシグナルを付ける」と対）。fail-open を選ぶのは、寸法不明のまま座標射影して**誤った位置を決定論削除する**方が危険だからである。
- `page_count` は `len(state.get("pages", []))` をループ前に 1 回算出して使う。

### 4.7 後方互換

- region 無しスキーマ / exclude_regions 空 / スキーマレス run: 全経路 no-op。テンプレートレス運用に退行なし。
- `pages.width` / `height` が NULL の既存行でも `structure_ocr` / `vl_fallback` は落ちない（§4.6 の fail-open 規約。敵対的レビュー第2回 C26 / C34）。
- リリース順序は「サーバ（モデル + DTO）→ UI」を厳守（`extra="ignore"` により逆順は region が無音で落ちる）。
- **gateway / orchestrator-worker のデプロイ順序（敵対的レビュー第2回 C27 / C29）**: Phase 1 は 2 つの ECS サービスにまたがる。gateway（`db.py:684` の `model_dump()` 丸ごと）が先に出ると、その後に保存された全スキーマの fields JSONB に `"region": null` が入り、旧 orchestrator の `make_kie_extract`（`llm_nodes.py:32` → `kie.py:67` `json.dumps`）がそれをそのままプロンプトに載せる（`FieldDef` は `extra=ignore` なので落ちはしないが、§5.6 の「1 バイトも変わらない」保証が破れる）。対処は**両方**行う:
  1. **順序依存を消す（必須）**: `db.py` put_schema の直列化を「`region` が `None` の field では `region` キー自体を書かない」にする（例: `f.model_dump(exclude={"region"} if f.region is None else None)`）。**`exclude_none=True` の全体適用は不可** — 既存の `"label": null` / `"columns": null` まで消えて、それ自体が現行プロンプトを変えてしまう。これで region 未設定スキーマの JSONB は現行と**完全同一 bytes** になり、旧 orchestrator と混在しても安全。
  2. **順序の明記（保険）**: それでも「実座標が設定された版」の除去は orchestrator 側 `_schema_for_prompt` が担うため、Phase 1 内は **orchestrator-worker（region キー除去）→ gateway（region 書込）** の順に出す。`scripts/aws_env.sh` の `cmd_push` は同一 image_tag で両サービスを force-new-deployment するがローリング完了順は保証しないため、Phase 1 では **API / chat から実座標を設定しない運用**とする（Phase 1 の単独価値は「契約の下地のみ」— §9）。

---

## 5. 抽出パイプラインの変更

### 5.1 新規モジュール `services/orchestrator/src/newfan_orchestrator/region_mask.py`

純関数・langgraph 非依存。面積計算は `packages/paddle_client/src/newfan_paddle_client/spans.py:25` の `_overlap_area` を流用（orchestrator は既に paddle_client に依存）。

```python
EXCLUDE_SPAN_RATIO = 0.5   # span 面積の過半が領域内なら除外
EXCLUDE_CELL_RATIO = 0.5   # セル面積の過半が領域内ならセルを空にする

def resolve_page(page: int | str | None, page_no: int, page_count: int) -> bool:
    # page==page_no / page=="last" and page_no==page_count / page is None → 適用。
    # int で page_count 超（存在しないページ）→ その run では不適用
def regions_for_page(exclude_regions, page_no, page_count,
                     page_w: int | None, page_h: int | None) -> list[BBox]:
    # 適用対象 rect を画素へ射影（round）
    # page_w / page_h が None または 0 以下 → [] を返す（射影不能。exclude 不適用の fail-open）。
    # 呼出側は metrics["region"]["skipped_pages_no_dims"] に page_no を積み無音化を防ぐ
    # （pages.width/height は DDL で nullable。既存テスト seed も寸法無し。§4.6 / 批評第2回 C26/C34）
def filter_spans(spans, px_regions) -> tuple[list[Span], int]:
    # (残存 spans, 除外件数)。overlap/area(span) >= 0.5 のいずれかで除外。
    # area(span)==0（poly_to_bbox の退化 bbox, spans.py:18-22）はゼロ除算になるため
    # 中心点包含判定にフォールバック（批評 tech-11）
def mask_tables(tables, px_regions) -> tuple[list[TableResult], MaskStats]:
    # セルは削除しない。被覆率>=0.5 のセルは value/span_ids を空にし bbox は残す
    # （検証 UI の列整合維持・オーバーレイ表示用。批評 product-8）。
    # 全セルが空になった行は行ごと削除（既存の「実データ行のみ残す」規則と同じで列ズレなし）。
    # 1 セルでもマスクした TableResult は structure_html を None 化
    # （pred_html 原文経由の漏洩遮断。web は rows しか描画しないため表示影響なし。批評 tech-3）
```

閾値の設計判断（維持）: 「交差即除外」は本文誤削除、中心点方式は境界跨ぎで不安定。面積比 0.5 は失敗の向きが安全側（かすり → 残す / 印影自身の OCR ゴミ → 全没で確実に落ちる）。UI では領域を対象より大きめに描く運用でカバー。

**既知の限界（ADR に明記）**: 表 OCR が印影文字をセル text（pred_html 由来）に混ぜ、かつセル被覆率が 0.5 未満のケースは決定論では消えない。混入値はレビューで人が直す。

### 5.2 exclude の適用点（ノードは増やさない）

ノード後段だと (1) checkpoint に印影テキストが永続化 (2) DD-02 backfill がゴミに再 OCR 課金 (3) quality_gate 誤発火 (4) VL 経路すり抜け、のためノード内部で適用する（維持）。

**`ocr_nodes.py` `make_structure_ocr`（:150-180 付近）** — ページループ内を以下の順に変更:

前提（ループ前に 1 回）: `page_count = len(state.get("pages", []))`。ページごとに `w, h = page.get("width"), page.get("height")`（**`page["width"]` の KeyError / None は `build_spans` 以降の非保護区間で送出され worker の再配信ループになるため、必ず `.get()` + §4.6 の fail-open 規約で処理する**。敵対的レビュー第2回 C26 / C34）。

```python
page_spans = build_spans(elem.pruned_result, page=page_no, start_id=next_span_id)
raw_span_count = len(page_spans)                     # ★フィルタ前件数を先に確保
w, h = page.get("width"), page.get("height")         # ★None 可（DDL nullable / 既存テスト seed）
px_regions = regions_for_page(state.get("exclude_regions", []), page_no, page_count, w, h)
# regions_for_page は w/h が None・0 以下なら [] を返す。その場合は
# metrics["region"]["skipped_pages_no_dims"] に page_no を積む（無音の素通しにしない）
page_spans, n_excluded = filter_spans(page_spans, px_regions)   # ★_backfill より前（再OCR課金防止）
if ocr_client is not None:
    _backfill(page_spans, data, ocr_client, backfill_threshold)
spans.extend(page_spans)
layout.extend(build_layout_blocks(elem.pruned_result, page=page_no))   # v1 フィルタなし（D9）
masked, stats = mask_tables(build_tables(elem.pruned_result, page_spans, page=page_no), px_regions)
tables.extend(masked)
next_span_id += raw_span_count                       # ★フィルタ後件数だと次ページと span_id 衝突
if elem.markdown is not None and elem.markdown.text and not px_regions:
    markdown_parts.append(elem.markdown.text)        # ★除外領域のあるページの markdown は落とす
```

- `build_tables` にフィルタ済み page_spans を渡す（span 由来の値復元・グラウンディングから除外 span を消す）+ `mask_tables` の 2 段構え（維持）。
- markdown skip の根拠と**トレードオフの正直な記述**（敵対的レビュー第2回 C24）: `layout_markdown` は span と独立の漏洩経路である。実サービング fixture（`packages/paddle_client/tests/fixtures/real_layout_parsing_sample*.json`）では markdown は全て空なので**現時点では no-op**だが、この「実害ゼロ」は fixture 2 件が空であることに依存しており、それ自体 `scripts/record_fixtures_local.py:59` のスタブ由来の可能性がある。**PP-StructureV3 が markdown を返す構成では、除外領域を持つページ（単ページ帳票では全文）の markdown が KIE 入力から丸ごと消える**（`layout_markdown` は `kie.py:65` の主要入力の 1 つ）。それでもページ丸ごと skip を維持するのは、`MarkdownResult` が座標を持たず**部分マスクが構造的に不可能**であり、§5.7 の除外保証（DB に載せない）を精度より優先するためである（KIE の根拠は span のみ・`kie_extract.yaml` 指示 1）。
  - 粒度は §5.7 の保証範囲注記にも明記する:「`layout_markdown` は**ページ単位**で落とす（部分除外ではない）」。
  - `state["metrics"]["region"]` に **`markdown_dropped_pages`**（page_no の配列）を追加し、運用で no-op か否かを観測可能にする。
  - 代替案（除外 span の text を markdown から行単位で除去し、残存時のみページ skip にフォールバック）は **Phase 4 の fixture 精度計測ゲートに載せて採否を判断**する（§9 Phase 4 / §11-10）。
  - 回帰テストには非空（34 文字）の合成 fixture `layout_parsing_response.json` を使う（批評 tech-14）。併せて `record_fixtures_local.py` の録画時に `results[0].markdown` を envelope に入れて実 markdown を fixture に残す（TODO）。

**`ocr_nodes.py` `make_vl_fallback`（:233-244 付近）** — 挿入位置を厳密に固定（批評 tech-7。`next_span_id +=` は既に extend より前にある）:

```python
vl_spans = build_spans(..., start_id=next_span_id, source=SpanSource.VL)
next_span_id += len(vl_spans)                        # ★フィルタ前に加算（既存行のまま）
vl_spans, n_excluded = filter_spans(vl_spans, px_regions)   # ★加算の後・extend の前に挿入
spans.extend(vl_spans)
layout.extend(build_layout_blocks(...))              # v1 フィルタなし（D9）
```

**除外件数の記録**: 両ノードは `state["metrics"]["region"]` に `{"excluded_spans": n, "excluded_cells": m, "excluded_rows": k}` を積算して返す（ExtractionState.metrics は既存キー）。

### 5.3 filter_blocks を実装しない（D9・草案から変更）

LayoutBlock フィルタ（閾値 0.9）は削除する。理由: (a) 被覆 0.5–0.9 のブロック `content` に除外テキストが残り「checkpoint からの除外保証」が成立しない（批評 tech-5）、(b) 原本 PNG・pages.image_uri がストレージに残る中で checkpoint だけ拭くのは中途半端（批評 product 過剰設計）、(c) layout は KIE プロンプトに渡らないため LLM 文脈への漏洩は無い。代わりに ADR に保証範囲を正確に書く（§5.6）。

### 5.4 除外の観測性（決定論削除には必ずシグナルを付ける）

- run metrics: `metrics["region"] = {excluded_spans, excluded_cells, excluded_rows, mismatch_fields, layout_mismatch, skipped_pages_no_dims, markdown_dropped_pages}`。永続化は `pg_persistence.save_result` の fallback_pages と同じ `metrics JSONB COALESCE || merge` パターン（:174-181）で、`save_result` に `region_stats: Optional[dict] = None` を追加。

  **配線は 4 点ではなく 5 点（敵対的レビュー第2回 C1 / C13 / C20 / C21）**: Protocol / InMemory / Pg の**3 実装** + 呼び出し側**2 点** = `db_nodes.make_finalize`（confirmed 経路、`db_nodes.py:36-44`）と **`worker.py` の needs_review 保存（`ExtractionWorker._process` の interrupt 停止分岐、`worker.py:148-158`）**。後者は `make_finalize` を通らず worker が直接 `save_result(..., fallback_pages=state.get("fallback_pages", []))` を呼ぶため、**`region_stats=state.get("metrics", {}).get("region")` を両呼び出し元で渡す**（fallback_pages と同じ配線箇所）。

  なぜ 5 点目が最重要か: §5.4 のセル/行マスク集約 ReviewItem は `route_confidence_gate`（`nodes.py:284`）で `hitl_review` へ送るため、**マスク発動 run は必ず `worker.py:150-158` の interrupt 経路で先に保存される**。ここを漏らすと `extraction_runs.metrics` に `region` が無く → `db.py:294` の `(row.metrics or {}).get(...)` が None → result API の `region_stats=None` → **§3.5 の除外バッジがレビュー中だけ表示されず**、ReviewItem の文言「Nセル/M行を未取込」だけが根拠なく浮く。resume 後の finalize（`db_nodes.py:36`）で初めて書かれるが、その時点でレビューは終わっている。span 除外のみの run は ReviewItem が付かないため、バッジが**唯一のシグナル**になる。

  `InMemoryContextStore.save_result` はキーワード引数省略でも通ってしまうため、**保存した `region_stats` を `saved_region_stats(run_id)` 等で観測可能にし、渡し漏れをローカルテストで検出できるようにする**。

- ReviewItem（`confidence_gate_node` で metrics を読んで積む — review_items の**生成箇所**を gate に一本化し、ocr ノード側で個別に積む二重経路を作らない）:

  **前提の訂正: 現行 gate は上流の ReviewItem を上書きで捨てている（既存欠陥・敵対的レビュー第2回 C12 / C23）**。`ExtractionState.review_items` は reducer 無しの LastValue チャネル（`extraction.py:95`）で、`confidence_gate_node` は state を読まず `return {"review_items": items}` で置換する（`nodes.py:214-224`）。このため `vl_fallback` が積む「VLフォールバック失敗 / VL結果なし（未抽出ページ）」ReviewItem（`ocr_nodes.py:221-229`、docstring `ocr_nodes.py:193` の意図どおり）は**グラフ通過時に既に消えている**（`test_ocr_nodes.py:103-104` はノード単体の戻り値しか見ておらず検出できない）。草案の「ocr ノードとのマージ競合を避ける」という記述は事実に反するため撤回する。
  **本設計では gate の戻り値を `list(state.get("review_items", [])) + gate_items` の carry-forward に改め**（`(field_name, reason)` で dedup）、この既存欠陥も同時に閉じる。**Phase 0 のスコープに含める**（§9）。`Annotated[list[ReviewItem], operator.add]` の reducer 導入は `apply_feedback` / resume 経路で重複蓄積するため採らない（§10-17）。

  **前提の訂正 2: ReviewItem は現状 UI に届かない（既存欠陥・敵対的レビュー第2回 C8 / C19）**。`review_items` は G2 ルーティングと interrupt payload にしか使われず、`save_result`（`pg_persistence.py:74`）は破棄する。検証画面の「要確認」は `extraction_fields.review_status` のみを見る（`db.py:284` / `page.tsx:74`）が、`review_status = PENDING` を立てるのは `llm_correct` 経路だけ（`llm_nodes.py:91`）で、`confidence_gate` の所見は画面に出ない。したがって:
  - **`confidence_gate_node` は ReviewItem を積む際、対応する field の `review_status = ReviewStatus.PENDING` も同時に立てる**（`nodes.py:214-224`。既存の gate 所見も同時に直る。`pg_persistence` の corrected/approved 保護 WHERE はそのまま有効）。**Phase 0 のスコープに含める**（§9）。
  - **対応 field を持たない run 単位の集約 ReviewItem**（セル/行マスク・span 20% 超）は永続化先が無いため、**`metrics["region"]` → `ResultResponse.region_stats` → §3.5 のバッジ/オーバーレイを唯一の到達経路と定義する**。よって「ReviewItem で『静かに消える』を防ぐ」という主張は「**run を needs_review に倒し、バッジ／オーバーレイで理由を出す**」に修正する。

  積む条件:
  - セル/行マスクが 1 件でも発生した run: 集約 1 件「除外領域により Nセル/M行を未取込（設定領域が明細に重なっていないか確認してください）」。
  - 除外 span 比率が全 span の 20% 超: 「除外領域が本文に重なっている可能性」1 件。
  - **除外 span 件数 ≥ 1 かつ `required` / `critical` フィールドの値が null（= grounding 喪失）**: 集約 1 件「除外領域が必須項目を消した可能性（オーバーレイを確認してください）」（敵対的レビュー第2回 C19。別レイアウト取引先で実データを消したケースの最低限の検知線）。
  - 通常の span 除外（印影ゴミ）は想定内動作なので ReviewItem なし・metrics のみ。
- 検証画面オーバーレイ + バッジ（§3.5）で査閲者が「画像にあるのに結果に無い」理由を知れる（批評 product-3）。

exclude を「レイアウト不一致 run でスキップする」案（批評 product-3a）は **v1 では採らない**。exclude の目的は「確実に DB に載せない」であり、shadow 運用中の不一致判定に決定論動作を依存させると保証が消える。また `filter_spans` は KIE 前・位置ガードは gate 後で**順序が逆**であり、v1 で結合すると実装が捻れる。上記の ReviewItem + バッジ + オーバーレイで「静かに消える」ことを防ぐ。
ただし**この判断は shadow 期間中のものである**（敵対的レビュー第2回 C19 / D18）: Phase 5（`REGION_GUARD_ENFORCE` on）で doc レベルの `layout_mismatch` が実測できるようになった後は、`REGION_EXCLUDE_SKIP_ON_LAYOUT_MISMATCH`（既定 off）フラグで「別レイアウトと判定した run では exclude を適用せず `metrics["region"]["skipped_exclude"]=true` を記録する」選択肢を運用者に用意する（§11-8）。

### 5.5 include 領域の位置ガード（confidence を触らない・shadow で出荷）

草案の「confidence_score で REGION_MISMATCH_CAP」は**撤回**する。グラフ順は `confidence_score → llm_correct → validate → confidence_gate`（`graph.py:96-100`）であり、cap は (a) `validate` の auto_elevate（`nodes.py:209-210`）に巻き戻され、(b) `llm_correct` の起動条件 `confidence < 0.80`（`llm_nodes.py:53`）を毎回踏んで文字補正 LLM が誤課金・値書換えリスクを負う（批評 tech-2）。

**最終仕様**（`nodes.py` `confidence_gate_node` 内、判定関数は region_mask.py に置く）:

1. 判定対象: region 付き・bbox 合成済み（F-0）のフィールド。
2. 判定: region を field.page の寸法で px 射影し、各辺方向に `max(ページ寸法の 5%, 領域の当該辺長の 50%)` 拡張した矩形に field bbox の**中心点**が入るか。
   **参照元の明記（敵対的レビュー第2回 C10 / C14 / C26 / C34）**: run のページ数は `len(state["pages"])`、テンプレート側は **`state.get("source_page_count")`**（§4.6 で配線）。page 判定は:
   - `source_page_count` が None（旧スキーマ・スキーマレス・未記録）→ **ページ数比較不能として page 判定を行わない**（mismatch 扱いにしない。`"last"` は従来どおり run の page_count に解決）。
   - `source_page_count` があり run の page_count と**異なる** → region.page が int の場合は page を**問わない**（ページ数可変帳票。批評 product-6/7）。
   - `field.page` の寸法（`pages[i].width/height`）が取れない → **当該フィールドの判定自体をスキップし mismatch に数えない**（§4.6 fail-open 規約。shadow / enforce いずれでも同じ）。
3. **v1（shadow mode）**: 不一致は `logger.info("region_mismatch field=%s ...")` + `metrics["region"]["mismatch_fields"]` に記録するのみ。**confidence も ReviewItem も触らない。**
4. 有効化（実測後・env フラグ `REGION_GUARD_ENFORCE`、既定 off）: region フィールドの**過半が同時 mismatch**（strict: `mismatch 数 > region 数 / 2`） → 「別レイアウトの帳票」と判定し、per-field レビューは抑止して `metrics["region"]["layout_mismatch"]=true` のみ（取引先 B の帳票を全件レビュー化させない — 批評 product-2 / scope E-14）。少数フィールドのみ mismatch → その項目にだけ `ReviewItem(reason="設定領域外の位置で検出")` **＋ 対応 field の `review_status = PENDING`**（§5.4 の既存欠陥修正が前提。これが無いと画面の「要確認」に出ない）。値はどの経路でも捨てない。

   **doc レベル判定の適用下限（敵対的レビュー第2回 C33）**: D2 のオプトイン設計では region 付きフィールドが 1〜2 件のスキーマが典型となる。n=1 ならその 1 件の mismatch は常に「過半」なので `layout_mismatch=true` → per-field レビュー抑止 → **ガードは metrics を書くだけで一度もレビューを出さない**（enforce しても shadow と挙動が変わらない）。n=2 も「1 件 mismatch＝レビュー / 2 件＝抑止」を n=2 のサンプルで判別する前提が成立しない。よって:
   - doc レベル判定は **region 付きフィールド数 n ≥ `REGION_GUARD_MIN_FIELDS_FOR_LAYOUT`（初期値 3）** の run にのみ適用する。
   - n ≤ 2 の run では doc レベル判定を行わず、mismatch した各フィールドに**そのまま per-field ReviewItem を積む**（少数 region スキーマではレイアウト差と誤抽出を統計的に区別できないため、値を捨てない前提で**レビュー側に倒す**）。
5. 許容パラメータ（5% / 50% / 過半 / `MIN_FIELDS_FOR_LAYOUT`）は region_mask.py の定数とし、shadow 実測で確定してから enforce する（§11-1。**region 件数 n の分布も shadow で実測する**）。

**hint のフォールバック規則（hard crop にしない、の実体・維持）**: 領域外に値があっても LLM は抽出してよい。領域はどの段でも値を捨てる根拠にならない。安全弁は上記ガード（有効化後）のみ。

### 5.6 KIE プロンプト

**Phase 1 で必須（プロンプト衛生・批評 scope B-5)**: Phase 1 以降に保存された全スキーマの fields JSONB には `"region": null` が入る（`db.py:684` は `model_dump()` 丸ごと）。`llm_nodes.make_kie_extract`（:32）の `schema_json=dict(state.get("schema", {}))` を `_schema_for_prompt(state.get("schema", {}))` に差し替える: deepcopy の上、**全 field から `region` キーを無条件除去**。これで region 設定の有無に関わらずプロンプトは現行と 1 バイトも変わらない（全文スナップショットテストで担保）。

**二重防御: gateway 側でも `null` の region キーを書かない（敵対的レビュー第2回 C27 / C29）**: 上記は orchestrator 側の防御線であり、**gateway だけ先にデプロイされた窓では旧 orchestrator が `"region": null` をプロンプトに載せる**。よって `db.py` put_schema の直列化も「`region` が `None` の field では `region` キー自体を書かない」に変更する（`exclude_none=True` の全体適用は既存の `"label": null` 等まで消して別のプロンプト差分を生むので**不可**）。これにより region 未設定スキーマの fields JSONB は現行と完全同一 bytes になり、デプロイ順序に依存しなくなる。実座標が設定された版の除去は依然 orchestrator 側 `_schema_for_prompt` が担うため、§4.7 のデプロイ順序規定も併せて維持する。テストは §8（`test_put_without_region_stores_no_region_key` / `test_kie_prompt_unchanged_without_regions` を「fields JSONB に `"region": null` を含む旧 orchestrator 想定入力」でも通す）。

**Phase 4（計測ゲート付き・批評 scope C-7）**: 現行プロンプトには座標が一切無く、ヒント注入の精度改善効果は未実証。以下を Phase 4 に分離し、`packages/paddle_client/tests/fixtures` ベースの精度計測（region 有無での field 正解率比較。**帳票タイトル等「位置でしか特定できない項目」の fixture を必ず含める** — 批評 product-5 の実測要求）を通過した場合のみ出荷する:
1. `_schema_for_prompt` を拡張し、region を持つ field にのみ `region_px: {page, bbox}`（当該ページ寸法で px int 射影。`"last"` は page_count に解決。**存在しないページを指す region はヒント自体を落とす**（1 ページ目への縮退は誤誘導 — 批評 tech-15 / product-11a））を注入。
2. `kie.py` `_spans_for_prompt` に bbox を追加 — **region 付き field が 1 つでもある場合のみ**（region 無しスキーマのトークン増ゼロ）。
3. `prompts/2026.07-1/kie_extract.yaml` に追記: 「region_px は項目がこの領域付近にある傾向を示すヒント。レイアウトが異なる帳票では領域外の正しい値を優先せよ。その場合も span_ids は実在 span を指すこと」。

### 5.7 VL / 画像経路の保証範囲（ADR 追補の文言・過大主張を排す）

ADR-0006 追補（`docs/adr/`）に以下を**正確に**書く（批評 tech-5 / scope B-6）:

> 除外領域が保証するのは「**span・テーブルセル値・layout_markdown・抽出結果（fields/tables）からの除外**」である。以下は保証外: (a) LayoutBlock.content と graph checkpoint への残存（v1 はスクラブしない）、(b) マスク非発動 TableResult の structure_html、(c) セル被覆率 0.5 未満での pred_html テキスト混入、(d) 自社ホスト視覚モデル（structure-svc / vl-svc）が画像として領域を見ること、(e) 領域設定以前の過去 run のデータ、(f) 原本・前処理後 PNG のストレージ残存、(g) `pages.width/height` が未登録のページ（寸法不明のため exclude を適用しない fail-open。§4.6。敵対的レビュー第2回 C26/C34）。なお `layout_markdown` の除外は**ページ単位**であり部分除外ではない（除外領域を持つページの markdown を丸ごと落とす。§5.2。敵対的レビュー第2回 C24）。画像を外部 LLM へ送る経路はコード上存在しない（kie / llm_correct はテキストのみ）。VL 由来 span は grounding 上限 0.7 で常にレビューを通る（DD-09）。
>
> 適用範囲についても正確に書く: 除外領域は **doc_type（スキーマ版）単位**で決定論的に適用される。同一 doc_type を共有する別レイアウトの取引先帳票にも同座標で適用されるため、その位置にある実データが除外され得る（D18 / §11-8）。またワークフローの `process.extract` は `schema_id`（版 id）固定であり、新版の除外設定は**既存ワークフローに自動適用されない**（D17 / §4.4b）。

---

## 6. API 変更

新エンドポイントは不要。既存の変更のみ。

| API | 変更 |
|---|---|
| `PUT /v1/admin/schemas`（`routers.py:721`） | `PutSchemaRequest.fields[].region?: RegionRect`、`exclude_regions?: RegionRect[] \| null`（**null=引き継ぎ / []=クリア**）、`source_page_count?: int \| null`（同セマンティクス）。RegionRect 違反・include の page:null は 422。create:true の E1005 既存挙動は不変。 |
| `GET /v1/admin/schemas`（`_schema_dto`） | `SchemaDto` に `exclude_regions: RegionRect[]`、`source_page_count?: int`、`fields[].region?` を追加。**応答忠実性がこの機能の生命線**（extra="ignore" による往復全滅の防止）。 |
| `GET /v1/documents/{id}`（`routers.py:164` / `db.py:144`） | `DocumentMeta` に **`pages: list[PageDim] = []`（既定値あり）**（`{page_no, width, height}`）を追加。未訪問ページの正規化に必須（批評 tech-4。草案 §8.10 は撤回）。**`DocumentMeta` は一覧 `GET /v1/documents`（`routers.py:143`）と共用のため、埋めるのは単体取得 `get_document`（`routers.py:170`）のみ**で、既存の `repo.get_pages(tenant_id, document_id)`（`db.py:159`）を 1 回呼ぶ（追加 SELECT 1 回）。一覧は `pages` を埋めず空のまま返す（**N+1 回避**。敵対的レビュー第2回 C25）。web 側も `pages?: PageDim[]` の任意型とし、`pageDims` は **`GET /documents/{id}` の応答からのみ**構築する。 |
| result API（`ResultResponse`, `dto.py:69` / `db.py:294,590`） | `region_stats: Optional[dict] = None`（`(row.metrics or {}).get("region")` — fallback_pages と同パターン。**`needs_review` 保存時点で非 None になること**が §3.5 バッジの前提 — §5.4 の save_result 5 点目）。<br>`applied_exclude_regions: list[ResolvedRegion] = []` — **RegionRect 生返しではなく、サーバ側でページ解決済みの `{page_no: int, rect, label?}` 配列**（敵対的レビュー第2回 C16）。理由: DocViewer は `pageNo` しか持たず `page_count` を持たないため、`"last"` / `null` の解決を web に再実装させると最終ページ限定の承認印除外が全ページに描かれる／描かれない事故になる。解決規則は §5.1 `resolve_page` と同一（`"last"` → page_count、`null` → 1..page_count に展開、page_count 超の int は落とす）。共通化のため `resolve_page` を `newfan_schemas` に置き orchestrator / gateway 双方から参照する。<br>**取得経路**: `db.py` の 1 SELECT 案は撤回し、`routers.get_result` に `admin: AdminRepository = Depends(get_admin)` を追加して `admin.get_schema_by_id(tenant, run.schema_id)` の `exclude_regions` を採る。Pg/InMemory 両実装に既存メソッドがあるため経路が一本化され、「InMemory では常に `[]` が返るので UI 実装者が本番との差異に気づけない」盲点が消える。`page_count` は `repo.get_pages()` の件数から取る。スキーマレス run は空。<br>`schema_doc_type: Optional[str] = None` — `run.schema_id` から `get_schema_by_id` で解決した doc_type（編集モードのプリロード起点。§3.1 / 敵対的レビュー第2回 C9）。上記 `get_schema_by_id` 呼び出しと同一 SELECT で取れる。 |
| `POST /v1/documents/{id}/extract`（`routers.py:350-355`） | **`ExtractRequest` に `supersede_review: bool = false` を追加**（敵対的レビュー第2回 C2/C3/C6）。true のときのみ競合判定を `repo.has_active_run`（`processing` + `needs_review`、`db.py:173-179`）から `has_processing_run` に切り替える（`chat_tools.rerun_extract` と同一意味論）。既定 false で外部連携の二重投入防止は不変。true で受理した場合、既存の `needs_review` run を **`superseded`** に遷移させる。`confirmed` / `exported` run を持つ帳票は従来どおり E1005 で拒否（確定済み結果の無警告置換防止）。**Phase 2 までに入れる**（Phase 3 の UI 単独先行は 409 トーストになる）。 |
| lint（`routers.py:901` / `lint.py`） | **L012（warning）「`extract.schema_id` が当該 doc_type の最新版ではありません（v1 / 最新 v3）」を追加**（§4.4b / D17。敵対的レビュー第2回 C5）。`schema_exists` と同型の注入関数 `schema_is_latest` を gateway から渡す（呼び出しに 1 引数追加）。**error ではなく warning**（既存ワークフローの有効化を塞がない）。 |
| ページ画像 API | 変更なし（既存の署名 URL 流用）。 |

web 側: `web/lib/types.ts` に `RegionRect` / `ResolvedRegion` / `PageDim`、`SchemaFieldDto.region?`、`SchemaDto.exclude_regions?/source_page_count?`、`DocumentMeta.pages?`（**任意・一覧では空**）、`ResultResponse.region_stats/applied_exclude_regions/schema_doc_type`。`web/lib/api.ts` の `putSchema` を `(docType, fields, opts?: {create?, excludeRegions?, sourcePageCount?})` に拡張（省略時はキー自体を送らない = 引き継ぎ）。さらに `api.getSchema(docType)`（既存 `GET /v1/admin/schemas/{doc_type}` = 最新版、`routers.py:707`）と `api.extract(id, {schema_id, supersede_review})` を追加し、`ExtractStart.pollJob` を **`web/lib/useExtractJob.ts`** に切り出して再抽出ボタンと共有する（§3.1。敵対的レビュー第2回 C3/C9/C11）。`TemplatizeSchema` の `onCreated` は `{docType, schemaId}` を渡す形に変更する（再抽出の `schema_id` 明示送信と編集モードの入口条件に必要 — C11 / C30）。

---

## 7. F-0: フィールド BBOX 修正の実装仕様

### 7.1 欠陥の実体（確認済み）

`services/llm_adapter/src/newfan_llm_adapter/kie.py:112-121` が `ExtractedField(span_ids=..., page=item.get("page"))` を bbox 未設定で作り、以降どのノードも埋めない → `pg_persistence.py` で常に NULL → DocViewer のフィルタで落ちる。明細だけ出るのは `build_tables` が構造由来 cell_box を持つため。

### 7.2 実装（kie.py の 1 箇所で貫通）

純関数 `_page_and_bbox(valid_ids, span_map, fallback_page)` を新設し、`_valid_span_ids` 確定直後（source_quote 合成と同じ場所）で呼ぶ:

1. `valid_ids` が空 → `(fallback_page, None)`。LLM 申告 page は残すが bbox は作らない（原文根拠の無い座標を捏造しない = span 根拠契約の延長）。
2. valid span を `s.page` でグループ化し**支配ページ**を選ぶ: span 数最多。同数タイは **valid span のうち `(page, span_id)` が辞書順最小のページ**（＝先に若いページ、同一ページ内は読み順先頭）。
   草案の「valid_ids の読み順先頭」は誤り（valid_ids は LLM 出力順で読み順保証が無い。批評 tech-13）。さらに**第 1 回で採用した「最小 span_id のページ」も跨ページでは読み順を表さない**（敵対的レビュー第2回 C35）: `span_id` の連番は単一 `build_spans` 呼び出し内（`spans.py:50-61`）でのみ保証され、`vl_fallback` は**全 OCR ページ確定後に `max(span_id)+1` から採番する**（`ocr_nodes.py:206, 233-236`）。このため 1 ページ目が VL フォールバックし 2 ページ目が OCR というケースでは、1 ページ目の VL span の方が大きい id を持ち、同数タイで 2 ページ目が支配ページになって「読み順先頭」という意図と逆になる。よって**第一キーを `page`** とする。
3. `bbox` = 支配ページ上の span のみの外接矩形 `[min x1, min y1, max x2, max y2]`（`tables.py:78` `_union_bbox` と同形）。ページ跨ぎ union は禁止（別画像座標の無意味矩形になる）。
4. `field.page` は LLM 申告でなく **span 由来で上書き**（申告 page は無検証で bbox と不整合になり得、DocViewer は `(f.page ?? 1) === pageNo` でフィルタするため誤ページに矩形が出る）。
5. 配置根拠は「span 根拠契約の執行点（valid_ids 確定・quote 合成）と同居するため」**のみ**とする。草案の「orchestrator 後段だと gateway チャット経路で再欠落」は現コードに該当経路が無く撤回（批評 tech-12）。
6. 草案 §1.2-5（LLM フォールバックテーブルの TableCell bbox 合成）は**やらない**。`kie.py` の LLM フォールバック TableResult は `page=None` で、DocViewer のセル矩形は `selectedCell.page === pageNo` 条件のため bbox だけ足しても描画されず効果ゼロ（批評 tech-9）。

### 7.3 下流変更ゼロ（維持・確認済み)

保存列は 0001 から存在し `pg_persistence.save_result` が既に INSERT。API → `types.ts` → `DocViewer.tsx` まで実装済みで値が入れば即表示。migration 不要。副次効果: `gate.py`（ReviewItem.page/bbox, :63-66）と chat の explain_field も同時に直る。§5.5 の位置ガードと §3.4 のゴースト表示の土台。

---

## 8. テスト計画

### ユニット

| ファイル | テスト |
|---|---|
| `services/llm_adapter/tests/test_kie.py` | `test_field_bbox_single_span`（bbox=span bbox・page=span page）/ `test_field_bbox_union_same_page` / `test_field_bbox_multipage_dominant_page`（支配ページのみ union・LLM 申告 page 上書き）/ **`test_field_bbox_tie_breaks_by_min_page_then_span_id`**（1 ページ目 VL span（大きい span_id）+ 2 ページ目 OCR span（小さい span_id）のタイで **1 ページ目**が選ばれること。C35）/ `test_field_bbox_empty_span_ids_keeps_reported_page_no_bbox` |
| `services/orchestrator/tests/test_region_mask.py`（新規） | `test_resolve_page_last_and_null` / `test_project_rounding` / `test_region_beyond_page_count_not_applied` / `test_filter_spans_ratio_boundary`（かすり=残る / 全没=落ちる / ちょうど 0.5）/ `test_filter_spans_zero_area_falls_back_to_center` / `test_mask_tables_empties_cell_keeps_column_alignment`（セルは残り value/span_ids が空・bbox 維持）/ `test_mask_tables_drops_fully_emptied_row` / `test_mask_tables_keeps_cell_with_corner_stamp` / `test_mask_tables_nullifies_structure_html_on_mask` / **`test_regions_for_page_returns_empty_when_page_dims_missing`**（width/height が欠落・None・0 のいずれでも `[]`。C26/C34） |
| `services/orchestrator/tests/test_ocr_nodes.py`（追補） | `test_structure_ocr_filters_spans_before_backfill` / `test_span_id_no_collision_across_pages_with_filter`（raw_span_count 加算）/ `test_vl_fallback_filters_after_id_advance`（複数 fallback ページで衝突なし）/ `test_markdown_skipped_on_excluded_page`（**合成 fixture `layout_parsing_response.json`（markdown 非空）を使用** — 実録 fixture は空で回帰が見えない）/ **`test_exclude_noop_when_page_dims_missing`**（既存 seed 形式 `{"page_no","image_uri"}` のまま exclude 有り run が例外なく完走し、`metrics["region"]["skipped_pages_no_dims"]` が立つ。C26/C34） |
| `services/orchestrator/tests/test_llm_nodes.py`（追補） | `test_kie_prompt_unchanged_without_regions`（**プロンプト全文スナップショット**。`"region": null` を含む旧 gateway 想定入力でも通ること。C27/C29）/ `test_region_key_stripped_from_schema_prompt`（`"region": null` が載らない）/ `test_state_schema_not_mutated`（deepcopy） |
| `services/orchestrator/tests/test_gate.py` / `test_nodes.py`（追補） | `test_region_guard_shadow_records_metrics_only`（confidence・review_items 不変）/ `test_region_guard_enforced_single_field_reviews` / `test_region_guard_enforced_majority_mismatch_suppresses_per_field`（**n≥3 の fixture を明記**）/ **`test_region_guard_layout_judgement_requires_min_fields`**（n=1 / n=2 では doc レベル抑止が働かず per-field ReviewItem が出る。C33）/ `test_region_guard_page_count_drift_ignores_page`（**`seed_run(source_page_count=2)` で 3 ページ run。state に `source_page_count` を積む前提**。C10/C14）/ **`test_region_guard_no_source_page_count_skips_page_judgement`**（None なら page 判定しない）/ `test_mask_stats_emit_aggregated_review_item` / **`test_gate_carries_forward_vl_fallback_review_items`**（vl_fallback 由来の未抽出ページ ReviewItem が gate 通過後も残り、`route_confidence_gate` が hitl_review を返す。C12/C23）/ **`test_review_items_mark_fields_pending`**（gate が ReviewItem を積んだ field の `review_status` が PENDING になり `review_summary.pending` に数えられる。C8）/ **`test_excluded_span_with_null_required_field_emits_review_item`**（C19） |
| `services/gateway/tests/test_schema_regions.py`（新規） | `test_put_get_roundtrip_region_and_exclude`（完全一致 — DTO 落ち検知）/ `test_legacy_put_without_exclude_key_inherits`（**None=引き継ぎ**。旧形式 body で新版を作っても exclude_regions/source_page_count が保全。**PUT 応答と GET の両方で assert** — C28）/ `test_explicit_empty_clears` / `test_rect_validation_422`（範囲外・x1>=x2・include の page:null）/ `test_chat_update_schema_preserves_regions` / `test_old_schema_returns_null_region_empty_excludes` / **`test_put_without_region_stores_no_region_key`**（保存 JSONB に `region` キーが存在しない。C27/C29）/ **`test_edit_mode_roundtrip_preserves_required_critical_columns`**（編集モード相当の body で新版を作っても `required` / `critical` / `columns` / 未知キーが保全。C4）/ **`test_result_applied_exclude_regions_resolves_last_and_null`**（3 ページ帳票で `"last"` → `page_no=3`、`null` → 1..3 展開、page_count 超は落ちる。InMemory 経路でも非空であること — C16） |
| `services/gateway/tests/test_extract_review.py`（追補） | **`test_extract_needs_review_rejected_by_default`**（既定 false で従来どおり E1005）/ **`test_extract_supersede_review_accepts_needs_review`**（202 で新 run。旧 run が `superseded`）/ **`test_extract_supersede_review_still_rejects_processing`** / **`test_extract_rejects_confirmed_document`**（C2/C3/C6） |

### 統合（Pg・「InMemory は通るが本番だけ壊れる」型の防止）

- `services/gateway/tests/test_pg_repository_integration.py`: DDL 整合プローブに `exclude_regions` / `source_page_count` の往復 assert を追加（明示列挙 SQL の列漏れ再発防止線）。**加えて `test_pg_put_schema_legacy_put_inherits_exclude_regions`（新規・実 Pg）**（敵対的レビュー第2回 C22）: 同一 tenant/doc_type に対し ① `exclude_regions=[r1], source_page_count=2` で put（v1）→ ② `exclude_regions=[r2]` で put（v2）→ ③ 引数なし（旧 UI 相当の legacy 呼び出し）で put（v3）→ **v3 が r1 ではなく r2 を引き継ぐ**こと（＝`ORDER BY version DESC LIMIT 1` の最新版 SELECT であること）、④ さらに引数なしで put（v4）でも r2 が保たれること、⑤ 明示 `[]` を渡した版でクリアされること、⑥ 別 tenant / 別 doc_type の版が混入しないこと、⑦ **PUT 応答の `SchemaRecord` が引数由来ではなく引き継ぎ後の実値を持つこと**（C28）を assert。※ 現行の InMemory ユニット `test_legacy_put_without_exclude_key_inherits` は `db.py` の SQL を通らないため、この経路は Pg でしか守れない。**`DATABASE_URL_TEST` が CI で設定されていることを Phase 1 の前提に含める**。
- **`services/orchestrator/tests/test_pg_load_context_integration.py`（新規・`DATABASE_URL_TEST` ゲート）**（批評 scope B-3 / 敵対的レビュー第2回 C15）: 既存 `test_pg_context_schema.py` は `EMPTY_SCHEMA` が `FieldSchema` として妥当かを見るだけの**単体テストで DSN も `load_context` も使わない**（`grep -rn load_context services/orchestrator/tests` はヒット 0）。すなわち `load_context` の SELECT は現在どのテストにも守られておらず、migration 0007 未適用の DB に新 orchestrator を出すと `SELECT ..., exclude_regions` が UndefinedColumn で全 run 失敗するが CI では検出されない。よって新規ファイルを起こす: `test_pg_save_result_integration.py` の `env` fixture（tenants / documents / extraction_runs のシードと後始末）を **pages と field_schemas（`exclude_regions` / `source_page_count` 込み、`scripts/e2e_real.py:91` の INSERT 形）の投入まで拡張**（または共通 conftest へ移動）した上で、「field_schemas → `PgContextStore.load_context` → `LoadedContext.exclude_regions` / `.source_page_count` → `db_nodes` → state」の**実 Pg 貫通**と、**schema dict に exclude / source_page_count が混入していないこと**（プロンプト汚染防止）、`schema_id` が NULL の EMPTY_SCHEMA 経路で `[]` / `None` になることを assert する。**配置は Phase 2 ではなく Phase 1 必須**（`load_context` の SELECT 列追加が Phase 1 に含まれるため、同 Phase で守らないと §4.5 の「列追加後すぐ SELECT へ追加」の再発防止線が 1 Phase 空く）。
- `services/orchestrator/tests/test_pg_save_result_integration.py`: フィールド bbox が NULL でなく保存されること、`region_stats` が metrics JSONB にマージされること。**`needs_review` 保存時点で `extraction_runs.metrics.region` が存在すること**（C1/C13/C20/C21）。
- `services/orchestrator/tests/test_worker.py`: exclude あり run で checkpoint/state に除外 span が無い・quality_gate の平均 conf がフィルタ後で計算されること。**加えて `test_needs_review_save_includes_region_stats`**（敵対的レビュー第2回 C1/C13/C20/C21）: exclude でセルマスクが発生した run が `hitl_review` で interrupt 停止した**その時点**で store の run metrics に `region` が載り、`GET /result` の `region_stats` が非 None になること（`test_fallback_pages_persisted` と同型）。`InMemoryContextStore.save_result` は `region_stats` を `saved_region_stats(run_id)` で観測可能にし、**キーワード引数の渡し漏れが素通りしない**ようにする。既存 seed が寸法を持たない点を踏まえ、**width/height 有り・無しの両方**を seed して両系統を通す。

### E2E / 手動

- `scripts/e2e_real.py`: region/exclude 付きスキーマで抽出 → 検証画面でテキストフィールド BBOX 表示・除外バッジ表示。
- web: tsc + 手動チェックリスト。既存項目（描く / ゴースト確定 / 置換 / 消す（input フォーカス中の Delete で矩形が消えないこと）/ モード切替 / 全ページタブ / "last"・全ページ exclude / 署名 URL 失効後の再取得 / `.tpl-overlay` 存在下で Ctrl+Enter 無効 / **矩形を触らず保存 → 現行と同一スキーマ** / 編集モードで既存領域のプリロードと新版警告）に、第 2 回レビューで以下を追加する:
  - **画像上（ゴースト・確定矩形の外）から 20px 以上ドラッグして矩形が確定すること**（ネイティブ画像ドラッグで pointercancel にならない。C31）
  - **ページタブ切替直後の即ドラッグで矩形が生成されないこと**／縦横比の異なるページへ切替直後のドラッグが旧ページ座標で保存されないこと（C18）
  - **include オフ行のゴースト確定 → 行が自動でチェックされること**／**領域を持つ行のチェック解除 → 矩形も消えること**（C32）
  - **プレビュー表示中の検証エラーがインライン表示され、トーストが幕の下に沈まないこと**（× で閉じられる・累積しない。C17）
  - **編集モードで矩形のみ追加して保存 → 新版 fields が region 以外 vN と完全一致**（required / critical / columns 含む）。table 型行が壊れないこと（C4）
  - **テンプレート化直後の同一帳票で（リロード後も）領域を編集して v2 保存できること**／**`schema_id` キー欠落応答で編集ボタンが出ないこと**（C7 / C30）
  - **同一帳票で 2 回連続編集 → 保存し、2 回目もプリロードされること**（doc_type 起点プリロード。C9）
  - **保存トーストの再抽出ボタン**: needs_review 状態で押して 409 にならず新 run が走り検証画面が更新される／`processing` 中は warn 表示／`confirmed` 状態ではボタンが出ない／**新 run の `schema_id` が新版 id と一致し `applied_exclude_regions` が保存した exclude と一致する**（C2/C3/C6/C11）
  - **保存成功トーストに「ワークフロー N 件が旧版を参照しています」が出ること**（該当がある場合。C5）
  - **exclude 保存前の警告文が出ること**（同 doc_type 全帳票に適用される旨。C19）

  さらに **Phase 3 実装後の QA レビューで再現した事故**を恒久チェック項目に加える:
  - **項目を選んでから画像の空きをドラッグ → その項目の読取領域になること**（背景 pointerdown で
    行の選択まで消すと、pointerup の時点で紐づけ先が無く「項目を選んでください」と出て
    永久に読取領域を描けない）
  - **`GET /documents/{id}` が失敗する／未応答のまま編集モードを開いて保存しても、既存の
    region / exclude_regions / source_page_count が保たれること**（ページ寸法が無いと
    正規化⇄画素の変換ができず矩形を復元できないが、落とすと全置換保存で全消去になる。
    編集不能なぶんは原本のまま持ち回して戻す）
  - **矩形を触らず保存したとき、rect が 1 桁も変わらないこと**（正規化 → 画素 round → 正規化の
    往復で 0.5px の丸めが乗り、開いて保存するたびに座標がずれていく）
  - **編集モードで `source_page_count` が書き換わらないこと**（編集画面を開いた帳票の
    ページ数で上書きすると、触っていないのに位置ガードのページ判定条件が変わる）
  - **既存スキーマに英字始まりでない項目名があっても編集・保存できること**（chat の項目追加は
    任意の名前を通すので、命名規則は新しく付けた／変えた名前にだけ課す）

  JS テスト基盤の導入は本件スコープ外（現状維持）。

---

## 9. 段階的リリース

| Phase | 内容 | 依存・順序 | 完了条件 |
|---|---|---|---|
| **0** | F-0（kie.py `_page_and_bbox` + test_kie 追補）<br>**＋ 第 2 回レビューで露見した既存バグの修正**（敵対的レビュー第2回 C8 / C12 / C23）: (a) `confidence_gate_node` の戻り値を `list(state.get("review_items", [])) + items` の **carry-forward** に変更（`vl_fallback` の「未抽出ページ」ReviewItem がグラフ通過で消える欠陥の修正）、(b) `confidence_gate_node` が ReviewItem を積んだ field の **`review_status = PENDING`** を同時に立てる（gate 所見が検証画面の「要確認」に出ない欠陥の修正） | なし。単独リリース可。(a)(b) は region 機能に非依存で、本設計の観測性の**前提**（先に入れる） | 検証画面でテキストフィールドに BBOX が出る。test_pg_save_result_integration で bbox 非 NULL。**gate 由来の所見が「要確認」に出る**・**vl_fallback の ReviewItem が interrupt payload に残る** |
| **1** | 保存契約: migration 0007 → RegionRect（newfan_schemas 一元定義）→ records/dto/db/admin/routers（**None=引き継ぎ / 戻り値も引き継ぎ後の実値** C28 / **region が None なら JSONB に region キーを書かない** C27・C29）→ LoadedContext/pg_persistence/db_nodes/ExtractionState 配線（**exclude_regions と `source_page_count` の両方** C10・C14）→ **llm_nodes の region キー除去 + プロンプト全文スナップショット** → DocumentMeta.pages 寸法（**単体取得のみ充填** C25）→ test_schema_regions / DDL プローブ追補 / **Pg 多版引き継ぎテスト**（C22）/ **`test_pg_load_context_integration.py`（SELECT 列追加と同一コミット）**（C15） | migration はコード変更より**先**。dto は records と**同一コミット**。**orchestrator-worker（region キー除去）→ gateway（region 書込）**の順（§4.7。逆順は旧 orchestrator が `"region": null` をプロンプトに載せる） | region/exclude が保存・往復・引き継ぎされ（**引き継ぎは実 Pg の多版シナリオで検証**）、**プロンプトと抽出挙動が現行と 1 バイトも変わらない**こと。**`DATABASE_URL_TEST` 有効な CI で `test_pg_load_context_integration` が緑**。単独価値は「契約の下地」のみ（chat/API から領域を実用的に設定できるという主張はしない — 批評 scope C-8 / product-11b。Phase 1 では API/chat から実座標を設定しない運用とする） |
| **2** | 除外パイプライン + shadow ガード: region_mask.py（**寸法欠落 fail-open 込み** C26・C34）/ ocr_nodes 2 箇所 / markdown skip（`markdown_dropped_pages` 記録）/ mask_tables / **save_result `region_stats`（呼び出し 2 経路: `db_nodes.make_finalize` と `worker.py` の needs_review 保存）** C1・C13・C20・C21 / confidence_gate_node の shadow 判定と集約 ReviewItem / ResultResponse 拡張（`region_stats` / **ページ解決済み** `applied_exclude_regions` / `schema_doc_type`）C16・C9 / **`POST /extract` の `supersede_review` 追加**（C2/C3/C6。UI より先に入れる）/ **lint L012**（C5）/ Pg 貫通テスト | 1 の後 | exclude が実際に効き、除外件数が metrics・result API・ReviewItem に出る（**`confirmed` / `needs_review` の双方で**）。region_mismatch が shadow で記録される。needs_review の帳票から `supersede_review` 付き再抽出が 202 になる |
| **3** | UI: TemplatizePreview / RegionCanvas / usePageImage（**ページ切替時 url null 化** C18）/ TemplatizeSchema 改修（`onCreated({docType, schemaId})`）/ **編集モード導線（doc_type 起点プリロード + `base` スプレッド保全 + 作成直後も入れる入口条件）** C4・C7・C9・C30 / DocViewer オーバーレイ + バッジ / **再抽出ボタン（`useExtractJob` 共有・条件付き表示・`schema_id` 明示）** C3・C11 / ワークフロー旧版参照の警告表示（C5）/ types.ts / api.ts / globals.css（**`.toast-wrap` の z-index を 200 へ** C17） | **2 の後（並行禁止** — UI が先だと保存してもサイレント no-op。批評 scope C-9）。サーバデプロイ後に UI（extra="ignore"）。**`supersede_review` が Phase 2 で出ていること**が前提 | 受け入れ条件 §3.4 を満たす。作成・編集の両モードが動く。**テンプレート化元の帳票からリロード後も編集できる** |
| **4** | KIE ヒント: `_schema_for_prompt` の region_px 注入 / `_spans_for_prompt` の条件付き bbox / kie_extract.yaml 追記 / **markdown の部分除去方式（除外 span の text を行単位で除去し、残存時のみページ skip）の採否判断**（C24） | 1 の後、いつでも。**fixture 精度計測（タイトル等の位置特定型フィールドを含む）で改善が確認できた場合のみ出荷** | 計測レポートと region 無しスキーマのプロンプト完全一致の両立。**markdown 有効化時の精度影響も同計測に含める** |
| **5** | ガード有効化: shadow 実測に基づき許容パラメータ（5% / 50% / 過半 / **`MIN_FIELDS_FOR_LAYOUT`**）を確定し `REGION_GUARD_ENFORCE` を on | 2 の実測後 | 誤 mismatch 率が許容水準（doc レベル判定込みで実測決定）。**n≤2 のスキーマで per-field レビューが出ること**（C33） |

---

## 10. やらないこと（理由付き）

1. **hard crop / 座標テンプレートマッチング**: ADR-0006 の「同 doc_type でもレイアウト差がある」前提に反し、誤配置帳票で値が静かに消える。領域は hint + 観測 + レビュー誘導のみ。**exclude も同じ前提に立つ**（同 doc_type の別レイアウトで実データを消し得る）ため、v1 は保存前警告 + オーバーレイ + 「除外 span > 0 かつ required/critical が null」の集約 ReviewItem で**検知に倒し**、レイアウト不一致時の exclude 降格は Phase 5 以降のフラグ（`REGION_EXCLUDE_SKIP_ON_LAYOUT_MISMATCH`）として用意する（§5.4 / §11-8。敵対的レビュー第2回 C19）。
2. **発見済み bbox の自動 region 化**: デフォルトで全フィールドに region が付くと製品が座標テンプレート必須品に退行する（批評 product-1）。ゴースト参考表示 + 明示確定のオプトインのみ。
3. **画像リダクション（領域白塗り）**: hard mask はレイアウト違いの帳票で実データを不可視化する。将来の領域単位 opt-in（`redact: true`）として設計余地のみ残す。
4. **filter_blocks（LayoutBlock フィルタ）**: 保証として中途半端（§5.3）。ADR の正直な保証範囲記述で代替。
5. **フィールド別ハードフィルタ / マルチコール化**: single-call 一括抽出の構造上不可能で、本件の目的に過剰。
6. **`region.mode: "strict"`（領域内 span の決定論読取）を v1 で実装**: span 根拠契約とは整合する有効な将来案だが、まず Phase 4 の計測で「タイトル系がヒントで取れる率」を実測してから判断する。fields JSONB へのキー追加は後方互換なので v2 で追加可能（批評 product-5 は「実測を先に」の要求として吸収）。
7. **LLM フォールバックテーブルの TableCell bbox 合成**: page が無く描画に届かない効果ゼロの変更（批評 tech-9）。
8. **矩形の移動・四隅リサイズハンドル / ズーム / Undo・Redo**: 削除→描き直し・置換で代替可能。JS テスト基盤の無い現状で最もバグ密度の高い部位を落とす（批評 D-11/D-13）。ズームは明示の非ゴール。
9. **旧スキーマ編集画面への領域編集 UI**: スプレッド保全により往復で壊れないことを確認済み。編集はプレビュー（作成/編集モード）に一本化。読み取り専用バッジのみ任意。
10. **スキーマ一覧への mismatch 集計バッジ**: 集計クエリ基盤が必要。run 単位の region_stats / バッジ / ReviewItem で「見直しシグナルが運用者に届く」最低線は成立する（批評 product-9 は部分対処）。
11. **既存 run への一括遡及再抽出**: 別機能。保存 UI の「今後の取込にのみ適用」明示 + 当該帳票の再抽出ボタンで期待ギャップを埋める（批評 product-10 / scope C-10 は部分対処）。
12. **span_id の再採番**: 歯抜けは全参照が dict 経由で無害。再採番は checkpoint 再開・VL 継投との整合リスクのみ。
13. **spans プロンプト座標の量子化**: Phase 4 の計測とセットで判断。
14. **セル内部分マスク等の細粒度制御**: セル空化で「DB に載せない」目的は達成される。
15. **pg_persistence の EMPTY_SCHEMA / schema dict への exclude 追加**: プロンプト汚染経路になるため置き場を LoadedContext/state トップレベルに一本化（批評 scope B-4 により草案から削除）。
16. **ワークフロー固定版の自動追従（repoint / `ExtractConfig.doc_type` による最新版解決）**: 本件スコープ外（敵対的レビュー第2回 C5 / D17）。理由は「有効化済みワークフローの抽出挙動が、管理者の操作なしに別の版へ切り替わる」副作用が本設計の検証範囲を超えるため。v1 は**可視化**（保存トーストの旧版参照警告 + lint L012 warning）に留め、恒久策は別チケットで判断する。§4.4b。
17. **`ExtractionState.review_items` への `Annotated[list, operator.add]` reducer 導入**: `apply_feedback` / resume 経路で ReviewItem が累積蓄積するリスクがあるため採らない。**gate ノード内での明示 carry-forward** で対応する（Phase 0。敵対的レビュー第2回 C12 / C23）。
18. **`documents.doc_type` のテンプレート化時自動更新**: 再抽出は `schema_id` の明示送信で足りるため行わない（classify の `method="declared"` に乗せるかは §11-11 で別途検討。敵対的レビュー第2回 C11）。
19. **矩形の移動・リサイズを編集モードにだけ入れる等の例外**: v1 は作成・編集で同一操作セット（§10-8）。

---

## 11. リスクと未解決事項

1. **位置ガードの許容パラメータ（ページ 5% / 領域辺長 50% / 過半判定 / `MIN_FIELDS_FOR_LAYOUT`）は現時点で根拠のない初期値**。shadow mode の実測（`metrics["region"]["mismatch_fields"]` の分布、および **region 付きフィールド数 n の分布** — 敵対的レビュー第2回 C33）で確定してから enforce する。enforce 前に確定できないのは設計上の意図（実測なしに本番有効化しない）。**ページ数可変帳票の mismatch で実測が汚染されないよう、`source_page_count` の配線（§4.6）は Phase 1 で必ず入れる**（C10 / C14）。
2. **KIE ヒントの効果は未実証**。Phase 4 の計測で改善が出なければ出荷しない。その場合「位置でしか特定できない項目」の要求には v2 の strict mode で応える判断が要る（→ やらないこと 6）。
3. **pred_html 経由のセル text 汚染（被覆率 0.5 未満）は決定論では消えない**（§5.1 既知の限界）。頻度が高ければ v2 で「セル text から除外領域内 span 相当部分を差し引く」検討。
4. **fit 表示での小フィールド精密指定**: A4@250dpi を fit すると scale ≈ 0.35、最小矩形 8 表示 px ≈ 23 画像 px。日付・単価セル等の指定はやや粗い。ズーム要望が出たら v2。
5. **checkpoint / LayoutBlock.content への除外テキスト残存**は保証外として ADR に明記済み。テナントから「checkpoint も消せ」要求が出た場合は別途スクラブ設計が要る。
6. **編集モードの同時編集**: put_schema は楽観ロックが無く、admin 2 人が同時に編集モードで保存すると後勝ちの新版が積まれる（既存挙動と同じ）。version 表示 + 保存時警告で v1 は許容。
7. **shadow → enforce の運用移行**: フラグ on のタイミング・テナント別段階適用の要否は実測後に決める。
8. **exclude の適用単位が doc_type（スキーマ版）であることの副作用**（敵対的レビュー第2回 C19 / D18）: 現行の分類は宣言 doc_type とファイル名語彙のみで取引先を区別できないため、取引先 A の帳票で描いた「承認印」矩形が、同じ `doc_type=invoice` を使う取引先 B/C の帳票にも適用され、その座標にある印字値（合計・発行者・登録番号）の span が除去され得る。span 数件では §5.4 の 20% 条件にもセル条件にも掛からず、grounding 喪失で値が null になっても現状は「要確認」に出ない（→ Phase 0 の `review_status` 修正と「除外 span > 0 かつ required/critical が null」の集約 ReviewItem で最低線を張る）。運用者の回避策が「取引先ごとに doc_type を分ける」しかない状態は製品を座標テンプレート必須品へ退行させるため、**v2 でレイアウト単位のスコープ化（取引先 / レイアウト ID での適用条件、または本文分類の導入後に不一致 run で exclude を降格）を検討する**。v1 は保存前警告 + オーバーレイ + ReviewItem による検知に留める。
9. **ワークフローの版固定（D17 / §4.4b）**: v1 は可視化のみで、有効化済みワークフローは旧版の除外設定を使い続ける。運用者が警告と lint L012 を無視した場合「保存したのに印影が消えない」は残る。自動 repoint を入れるかは実運用での踏み方を見てから判断する。
10. **markdown 除外のページ粒度（敵対的レビュー第2回 C24）**: 現行 fixture では markdown が空なので実質 no-op だが、structure-svc が markdown を返す構成に変わると、除外領域を持つページ（単ページ帳票では全文）の KIE レイアウト文脈が丸ごと失われる。精度影響は Phase 4 の計測に含め、部分除去方式への切り替えを判断する。
11. **`documents.doc_type` はテンプレート化時に更新されない**（§10-18）。再抽出は `schema_id` 明示で足りるが、classify の `method="declared"` に乗せて自動取込側の推定精度を上げるかは別途検討する。
12. **`superseded` run status の追加（D15）**: `get_latest_run` / 削除ブロッカー / `workflow_graph.py:487` の hitl_gate / 一覧の絞り込みなど、`extraction_runs.status` を読む全経路の棚卸しが Phase 2 の実装時に必要。棚卸しを怠ると旧 run がレビュー待ちのまま残る。
13. **`"last"` 以外の相対ページ指定**（「2 ページ目以降」等）は要求が出てから。

---

## 付録: 批評対応表

| 指摘 | 対応 | 反映箇所 |
|---|---|---|
| product-1（デフォルト全 region 化） | 対処: ゴースト参考表示 + クリック確定のオプトイン。無操作保存=現行同一を受け入れ条件化 | §2 D2, §3.4 |
| product-2 / scope E-14（per-field cap のレビュー洪水・自己矛盾） | 対処: shadow mode 出荷 + 有効化時は doc レベル過半判定で per-field 抑止 | §5.5 |
| product-3（exclude の無音削除） | 対処: metrics + 集約 ReviewItem + DocViewer 読み取り専用オーバーレイ/バッジ。「不一致 run で exclude をスキップ」のみ不採用（exclude の保証が消えるため。理由 §5.4） | §5.4, §3.5 |
| product-4 / tech-8 / scope A-2（編集導線ゼロ） | 対処: 編集モード導線を Phase 3 必須化。なお「無警告で新版誕生」は create:true が E1005/409 で拒否されるため事実誤認と注記（routers.py:728-733） | §3.1, §2 D13 |
| product-5（strict mode） | 部分対処: v1 実装せず。Phase 4 計測に位置特定型 fixture を必須化し、結果次第で v2 追加（キー互換確認済み） | §5.6, §10-6, §11-2 |
| product-6（ページ数可変） | 対処: `page:"last"` を include/exclude 両対応、`source_page_count` 記録、page_count 相違時は page 不一致を問わない | §4.1, §4.3, §5.5 |
| product-7（5% 許容の根拠なし） | 対処: ゴースト確定時の自動パディング + 許容を領域サイズ比例併用 + shadow 実測後に確定 | §3.4, §5.5, §11-1 |
| product-8（セル削除の列ズレ） | 対処: 削除→空化に変更（bbox 維持） | §5.1 |
| product-9（観測性がログ止まり） | 部分対処: result API + 検証画面バッジ/オーバーレイ。スキーマ一覧集計は不採用（理由 §10-10） | §5.4, §6 |
| product-10 / scope C-10(遡及なし) | 部分対処: 「今後の取込のみ」明示 + 当該帳票の再抽出ボタン。一括遡及は不採用（§10-11） | §2 D15, §3.1 |
| product-11a / tech-15（page 超過の 1 縮退） | 対処: ヒント自体を落とす | §5.6 |
| product-11b / scope C-8（Phase 1 価値の過大主張） | 対処: 「契約の下地のみ」に修正 | §9 |
| product 過剰設計（cap 即時 / filter_blocks / リサイズハンドル / no-touch 手数） | 対処: shadow 化 / v1 削除 / v1 削除 / 受け入れ条件化 | §5.5, §5.3, §3.4 |
| tech-1 / scope A-1（exclude_regions の無音消失） | 対処: None=引き継ぎ / []=クリア。put_schema 内実装で旧 UI・chat 無変更のまま安全。保全テスト追加 | §4.4, §8 |
| tech-2（cap の auto_elevate 巻き戻し・llm_correct 誤発火） | 対処: cap 廃止、confidence_gate_node 内判定・confidence 不変 | §5.5 |
| tech-3（structure_html 漏洩・「値は汚れない」過言） | 対処: マスク発動時 None 化 + 限界を ADR/設計に明記 | §5.1, §5.7 |
| tech-4（未訪問ページの正規化不能） | 対処: DocumentMeta.pages 追加（草案 §8.10 撤回） | §6 |
| tech-5 / scope B-6（checkpoint 保証の過大・VL 側 layout 漏れ） | 対処: filter_blocks 自体を v1 削除し、ADR で保証範囲を正確化（VL 側 layout も同扱い） | §5.3, §5.7 |
| tech-6（exclude の観測非対称） | 対処: metrics + 閾値/発生ベースの ReviewItem | §5.4 |
| tech-7（VL span_id 衝突） | 対処: 挿入位置を「`next_span_id +=` の後・extend の前」に固定 + テスト | §5.2, §8 |
| tech-9（フォールバックテーブル bbox が効果ゼロ) | 対処: 項目自体を削除 | §7.2-6, §10-7 |
| tech-10（name 紐付け・include=false・Delete） | 対処: rowId 紐付け / **include=false 行の region は保存対象外（第2回 C32 により強化: ゴースト確定で include を自動オン / include 解除で region も連動削除し、UI 上の不整合状態を作らない。保存時フィルタは防御的二重化）** / typing ガード | §3.3, §3.4 |
| tech-11（ゼロ面積 span のゼロ除算） | 対処: 中心点判定フォールバック + 境界テスト | §5.1, §8 |
| tech-12（F-0 配置根拠の誤り） | 対処: 根拠を span 根拠契約の同居のみに差し替え | §7.2-5 |
| tech-13（タイブレークの誤り） | 対処: 最小 span_id のページに定義変更 | §7.2-2 |
| tech-14（markdown fixture の実態） | 対処: 回帰テストは合成 fixture `layout_parsing_response.json` を使用 | §5.2, §8 |
| tech-16（RegionRect 3 重定義） | 対処: newfan_schemas に一元定義し import | §4.1 |
| scope B-3（Pg 貫通テスト欠落） | 対処: test_pg_context_schema.py に exclude 貫通テストを Phase 2 必須で追加 | §8 |
| scope B-4（EMPTY_SCHEMA 矛盾） | 対処: 置き場を LoadedContext/state トップレベルに一本化、草案 §2.3 該当記述を削除 | §4.6 |
| scope B-5（`"region": null` プロンプト汚染） | 対処: region キー無条件除去を **Phase 1 必須**に昇格 + プロンプト全文スナップショット | §5.6, §9 |
| scope C-7（KIE ヒントの価値未実証） | 対処: Phase 4 分離 + 精度計測ゲート | §5.6, §9 |
| scope C-9（Phase 2/3 並行の危険） | 対処: 2→3 順序を強制事項化 | §9 |
| scope D-11（UI フル装備の見積もり） | 対処: 移動・リサイズ削除。ハイライトは単一 selection の派生に構造変更し同期機構を排除 | §3.3, §3.4 |
| scope D-12（Delete typing ガード） | 対処: page.tsx:137 同型ガードを仕様化 | §3.4 |
| scope D-13（ズーム・フォールド越えドラッグ） | 対処: fit 表示でスクロール問題を構造的に排除 + ズームを明示非ゴール + 精密指定の限界をリスクに記載 | §3.4, §10-8, §11-4 |

---

## 付録: 第2回レビュー対応表（C1〜C35）

凡例: **対処** = 設計を変更して閉じた / **部分対処** = v1 で一部のみ / **不採用** = §10 または §11 に理由付きで明記。

| # | 重大度 | 指摘 | 対応 | 反映箇所 |
|---|---|---|---|---|
| C1 | major | `needs_review` 経路の `save_result`（worker.py:150）が region_stats 追加対象から漏れ、レビュー中にバッジが出ない | 対処: 追加点を **4 点 → 5 点**に修正（Protocol / InMemory / Pg の 3 実装 + 呼び出し 2 点 = `db_nodes.make_finalize` と `worker.py:148-158`）。worker 側は `region_stats=state.get("metrics", {}).get("region")` を渡す。マスク発動 run が必ず interrupt 経路を通る根拠と、バッジ表示の前提であることを明記。InMemory の観測 API とテストを追加 | §5.4, §3.5, §6 result API, §8（test_worker / test_pg_save_result）, §9 Phase 2 |
| C2 | major | D15 の再抽出が REST `/extract` の `has_active_run` で needs_review を 409 拒否 | 対処: 「既存 API 再利用」を**撤回**。`supersede_review: bool = false` を追加し true 時のみ `has_processing_run` 判定へ。`schema_id` は PUT 応答の新版 id を**明示送信**。サーバ変更は **Phase 2**、UI は Phase 3 | §2 D15, §3.1「再抽出ボタンの仕様」, §6 extract API, §8, §9 |
| C3 | major | 同上 + confirmed で確定値を無警告上書き + トーストの同期 onClick でポーリング/refetch なし | 対処: `confirmed`/`exported` とロック readOnly では**ボタンを出さない**。旧 needs_review run は `superseded` へ遷移。`ExtractStart.pollJob` を `web/lib/useExtractJob.ts` に切り出し共有、成功時に `invalidateQueries(['result', id])`、409 は warn | §2 D15, §3.1, §6, §8 手動チェック, §11-12 |
| C4 | major | 編集モードの DraftRow が required/critical/columns を持たず新版で全滅 | 対処: `DraftRow.base?: SchemaFieldDto` を追加し `{...base, name, label, type, region}` で**未知キーごと保全**。TYPE_OPTIONS に `table` 追加（型 select は読み取り専用）。受け入れ条件「region 以外 vN と完全一致」とテストを追加。D13 に役割分担を明記 | §2 D13, §3.2, §3.3, §3.4 受け入れ条件, §8 |
| C5 | major | ワークフローは `schema_id` 版固定のため新版が自動取込に一切効かない | **部分対処**: §4.4b を新設し D17 を追加。v1 は可視化のみ（保存トーストの旧版参照警告 + lint **L012** warning）。トースト文言を「手動抽出・分類推定には最新版」に正確化。自動 repoint は §10-16 で理由付き非スコープ、§11-9 にリスク記載 | §2 D17, §4.4b, §6 lint 行, §9 Phase 2/3, §10-16, §11-9 |
| C6 | major | C2 と同旨（needs_review で必ず 409・描く→再抽出→直す導線が成立しない） | 対処: C2/C3 と同一の `supersede_review` 経路で解決。旧 run の状態遷移（`superseded`）と、それを読む全経路の棚卸しを Phase 2 の作業として明記 | §2 D15, §3.1, §6, §11-12 |
| C7 | major | 編集モード入口 `schema_id !== null` はテンプレート化元の帳票では成立せず write-once | 対処: 入口条件を `typeof data.schema_id === "string" \|\| createdSchemaId !== null` に変更。`onCreated({docType, schemaId})` 化。受け入れ条件に「リロード後も同一帳票から編集して v2 保存できる」を追加 | §3.1, §3.4 受け入れ条件, §8 手動チェック |
| C8 | minor | ReviewItem は画面に届かない（`review_status` 未反映）— 既存バグ | 対処: 前提事実を §5.4 に明記した上で、**`confidence_gate_node` が ReviewItem 対象 field の `review_status = PENDING` を立てる修正を Phase 0 に追加**。対応 field を持たない集約 ReviewItem は `region_stats` → バッジを**唯一の到達経路**と定義し、「ReviewItem で静かに消えるのを防ぐ」という文言を「run を needs_review に倒し、バッジで理由を出す」に修正 | §5.4, §3.5, §9 Phase 0, §8 |
| C9 | minor | 「listSchemas から schema_id で取得」は不成立（一覧は doc_type ごと最新版のみ） | 対処: `ResultResponse.schema_doc_type` を追加し、web は `api.getSchema(docType)`（既存 `GET /schemas/{doc_type}` = 最新版）でプリロード。`schema_id` は版表示注記にのみ使用。取得失敗時は編集ボタン無効化 | §3.1, §6 result API, §8 手動チェック |
| C10 | minor | `source_page_count` が state に配線されず位置ガードが実装不能 | 対処: exclude_regions と**同じ 4 点**（LoadedContext / seed_run / load_context SELECT / db_nodes return / ExtractionState）に拡張。None なら page 判定を行わない縮退規則を明記 | §4.6, §5.5-2, §8, §9 Phase 1 |
| C11 | minor | 再抽出で新版 `schema_id` を明示しないと exclude が無音 no-op | 対処: `body.schema_id` に PUT 応答の `SchemaDto.id` を**明示送信**（classify / ExtractStart の推定は流用禁止）と D15 に明記。受け入れ条件に「新 run の schema_id 一致 + `applied_exclude_regions` 非空」を追加。`documents.doc_type` 自動更新は §10-18 で非スコープ | §2 D15, §3.1, §3.4, §8, §10-18, §11-11 |
| C12 | minor | `confidence_gate_node` が review_items を上書きし vl_fallback の項目を捨てている（既存バグ） | 対処: 「ocr ノードとのマージ競合を避ける」という**事実誤認の記述を撤回**し、`list(state.get("review_items", [])) + gate_items` の carry-forward（`(field_name, reason)` で dedup）へ。**Phase 0 のスコープに追加**。reducer 案は §10-17 で不採用理由を明記 | §5.4, §9 Phase 0, §8, §10-17 |
| C13 | minor | C1 と同旨（save_result 5 点目） | 対処: C1 と同一。InMemory の `saved_region_stats(run_id)` で渡し漏れを検出可能に | §5.4, §8 |
| C14 | minor | C10 と同旨（`source_page_count` 未配線） | 対処: C10 と同一。ページ数可変帳票で Phase 5 の実測が汚染される点を §11-1 に明記 | §4.6, §5.5, §8, §11-1 |
| C15 | minor | `test_pg_context_schema.py` は DSN 不使用の単体テストで、`load_context` の実 Pg テストは現状ゼロ | 対処: **`test_pg_load_context_integration.py` を新規**（`DATABASE_URL_TEST` ゲート）。`test_pg_save_result_integration.py` の `env` fixture を pages / field_schemas 込みに拡張。**配置を Phase 2 → Phase 1 必須**に前倒しし、完了条件に「CI で緑」を追加 | §8 統合, §9 Phase 1 |
| C16 | minor | `applied_exclude_regions` の生返しは DocViewer に `resolve_page` 再実装を強い、InMemory では常に `[]` | 対処: **サーバ側でページ解決済み `{page_no, rect, label?}`** を返す。取得を `routers.get_result` + `admin.get_schema_by_id` に一本化して Pg/InMemory の経路差を消す。`resolve_page` を `newfan_schemas` に置き双方から参照。展開規則のテストを追加 | §6 result API, §3.5, §8 |
| C17 | minor | warn トーストが `.tpl-overlay`（z-index 100）の下に沈み操作不能 | 対処: プレビュー内の検証・API エラーは**右ペインのインライン `role="alert"`** に表示し、トーストは閉じた後の成功通知に限定。加えて `.toast-wrap` の z-index を 200 に引き上げ（二重防御）。手動チェックに追加 | §3.2, §3.1（トースト表示位置）, §8, §9 Phase 3 |
| C18 | minor | ページ切替直後は旧画像・旧 scale のまま描画でき、誤ページ座標で保存される | 対処: §3.4 に「ページ切替中の描画禁止」行を新設（url の即 null 化 / `onLoad` 済み かつ `naturalWidth` が `pageDims[currentPage]` と一致するまで `pointer-events: none` / 進行中矩形の破棄 / scale の再計算）。`DocViewer.tsx:28-42` の現行挙動は**継承しない**と明記 | §3.4, §8 手動チェック, §9 Phase 3 |
| C19 | minor | exclude は doc_type 単位の決定論削除で、同 doc_type の別レイアウト取引先の実データが静かに消える | **部分対処**: D18 を追加。保存前警告文の必須化、「除外 span ≥1 かつ required/critical が null」の集約 ReviewItem 追加、`review_status` 修正（C8）を前提化。§5.4 の「不一致 run でスキップしない」は**shadow 期間中の判断**と限定し、Phase 5 以降は `REGION_EXCLUDE_SKIP_ON_LAYOUT_MISMATCH`（既定 off）で選択可能に。レイアウト単位スコープ化は §11-8 で v2 | §2 D18, §3.4 保存前警告, §5.4, §5.7, §10-1, §11-8 |
| C20 | minor | C1 と同旨 | 対処: C1 と同一。Phase 2 完了条件に「confirmed / needs_review の双方で件数が出る」を明記 | §5.4, §9 Phase 2 |
| C21 | minor | C1 と同旨（span 除外のみの run ではバッジが唯一のシグナル） | 対処: C1 と同一。バッジ（metrics 由来）とオーバーレイ（schema 由来）の**依存経路が異なる**点を §3.5 に注記 | §5.4, §3.5, §8 |
| C22 | minor | None=引き継ぎは Pg の SQL 依存なのに、Pg 側テストは明示値の往復のみ | 対処: **`test_pg_put_schema_legacy_put_inherits_exclude_regions` を新規**（多版シナリオ・最新版 SELECT・tenant/doc_type 境界・明示 `[]` クリア・PUT 応答の実値）。§4.4 に「同一トランザクション内 `ORDER BY version DESC LIMIT 1`」を実装制約として明記。Phase 1 完了条件に反映 | §4.4, §8 統合, §9 Phase 1 |
| C23 | minor | C12 と同旨（上書き return による carry-forward 欠如） | 対処: C12 と同一。既存欠陥をスコープ外にせず **Phase 0 で閉じる**判断を明記 | §5.4, §9 Phase 0, §10-17 |
| C24 | info | `layout_markdown` のページ丸ごと skip は no-op か有害の二値 | **部分対処**: 「実害ゼロ」の根拠が fixture 2 件の空 markdown に依存することを明記し、有効化時の精度トレードオフを正直に記述。粒度を §5.7 保証範囲に追記。`markdown_dropped_pages` を metrics に追加。部分除去方式は Phase 4 の計測ゲートに載せる。fixture 録画の TODO も記載 | §5.2, §5.7, §9 Phase 4, §11-10 |
| C25 | info | `DocumentMeta.pages` 必須型化で一覧 API が N+1 | 対処: `pages: list[PageDim] = []` の**既定値あり**とし、埋めるのは `get_document` のみ（`repo.get_pages` 1 回）。一覧は空のまま。web も `pages?` 任意型 | §2 D14, §6 |
| C26 | info | `pages.width/height` が NULL/欠落時の `regions_for_page` 挙動が未定義（ノードごと落ちて再配信ループ） | 対処: §4.6 に fail-open 規約を新設（`.get()` で取得 / None・0 以下なら exclude 不適用・位置ガードもスキップ / `skipped_pages_no_dims` に記録して無音化しない）。§5.1 の関数契約と §5.2 のコード片に反映。§5.7 保証外 (g) に追記 | §4.6, §5.1, §5.2, §5.5-2, §5.7, §8 |
| C27 | info | Phase 1 内で gateway が orchestrator より先に出るとプロンプトが変わる | 対処: **順序依存を消す**（`region` が None の field では JSONB に `region` キーを書かない。`exclude_none=True` は不可の理由も明記）+ **順序の明記**（orchestrator → gateway）の両方。Phase 1 では実座標を設定しない運用も明記 | §4.7, §5.6, §9 Phase 1, §8 |
| C28 | info | `put_schema` の戻り値が引数由来で、引き継いだ値が PUT 応答に載らない | 対処: 戻り値に**INSERT した確定値**を載せ「PUT 応答 = 直後の GET」を規定。旧編集画面が応答で state を差し替える経路（見かけ上の消失 → 次の保存で本当にクリア）を明記。テストを PUT 応答と GET の両方 assert に具体化 | §4.4, §8 |
| C29 | info | C27 と同旨 | 対処: C27 と同一 | §4.7, §5.6, §9 Phase 1 |
| C30 | info | 入口条件 `schema_id !== null` は page.tsx の厳密比較方針に反し、undefined で誤表示 | 対処: `typeof data.schema_id === "string"` に変更（undefined は出さない側へ）+ `createdSchemaId` による作成直後の導線。手動チェックに「`schema_id` キー欠落応答で編集ボタンが出ないこと」を追加 | §3.1, §8 手動チェック |
| C31 | info | ステージ上の `<img>` のネイティブドラッグで `pointercancel` が発火し矩形が消える | 対処: `draggable={false}` / `pointerdown` 先頭と `onDragStart` で `preventDefault()` / `user-select: none; -webkit-user-drag: none;` を §3.4「表示」に明記。手動チェックに「画像上から 20px ドラッグして確定すること」を追加 | §3.4, §8 手動チェック |
| C32 | info | `include=false` 行のゴーストを確定でき、保存時に無音で落ちる | 対処: ゴースト確定時に `include=true` へ自動切替、include 解除時は region も連動削除（§3.4 に「include 解除」行を新設）。§3.3 に不変条件（保存時フィルタは防御的二重化）を明記。付録 tech-10 の対応欄も更新 | §3.3, §3.4, §8 手動チェック, 付録 tech-10 |
| C33 | info | 「region フィールドの過半 mismatch」判定は n=1〜2 の典型スキーマで per-field レビューを構造的に出せない | 対処: `REGION_GUARD_MIN_FIELDS_FOR_LAYOUT`（初期値 3）を導入し、n ≤ 2 では doc レベル判定を行わず per-field ReviewItem を積む（値を捨てない前提でレビュー側に倒す）。「過半」を strict 定義に明確化。n の分布も shadow 実測対象に追加 | §5.5-4/-5, §8, §9 Phase 5, §11-1 |
| C34 | info | C26 と同旨（既存テスト seed が寸法を持たない） | 対処: C26 と同一。`test_exclude_noop_when_page_dims_missing` と、test_worker で width/height 有り・無し両方を seed する方針を追加 | §4.6, §5.2, §5.5, §8 |
| C35 | info | タイブレーク「最小 span_id のページ」は VL 併存時に読み順を表さない | 対処: タイブレークを **`(page, span_id)` の辞書順**に変更（第一キーを page）。`vl_fallback` が `max(span_id)+1` から採番する事実を根拠として明記。テスト名を `test_field_bbox_tie_breaks_by_min_page_then_span_id` に改名し VL 併存ケースを追加 | §7.2-2, §8 |
