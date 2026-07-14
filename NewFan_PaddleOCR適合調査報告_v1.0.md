# PaddleOCR 適合調査報告 v1.0

| 項目 | 内容 |
|---|---|
| 作成日 | 2026-07-14 |
| 対象 | `ai-ocr-agents/PaddleOCR`（公式リポジトリ clone, main ブランチ, PP-OCRv6 対応世代） |
| 準拠文書 | NewFan_AI-OCRエージェント詳細設計書 v1.1 |
| 目的 | 設計書の各コンポーネントに PaddleOCR の実装をどう適用するかの確定と、付録B「実装時要確認リスト」（C-1〜C-8）のコード実査による解消 |

---

## 1. リポジトリの素性

`PaddleOCR/` は **PaddlePaddle 公式リポジトリのクローン**であり、自社アプリケーションコードは含まれない。以下の資産で構成される。

| ディレクトリ | 内容 | 本システムでの扱い |
|---|---|---|
| `paddleocr/` | Python パッケージ本体。`_pipelines/`（PaddleOCR / PPStructureV3 / PaddleOCRVL / PPChatOCRv4Doc / PPDocTranslation 等のラッパー） | 推論層3サービスの中核 |
| `docs/version3.x/` | パイプライン仕様・**サービング REST API 仕様**（layout-parsing / ocr） | `packages/paddle_client` の型定義の一次資料 |
| `deploy/paddleocr_vl_docker/` | PaddleOCR-VL サービング一式（compose: gateway + pipeline + genai server、vLLM/FastDeploy 切替、`pipeline_config_vllm.yaml` は既定 PaddleOCR-VL-1.6） | **vl-svc の雛形としてそのまま流用可** |
| `mcp_server/` | MCP サーバ（local / self_hosted(HTTP) / aistudio / qianfan の4プロバイダ） | 参考実装（採用せず） |
| `langchain-paddleocr/` | LangChain document loader | 参考のみ（LangGraph ノードは自前実装が設計に合う） |
| `api_sdk/`（TS/Go） | 公式ホステッド API（AI Studio）用クライアント。ジョブ発行+ポーリング型 | 自ホストサービングとは API 形状が異なる。poller・型設計の参考 |
| `paddleocr-js/` | ブラウザ OCR SDK（npm: `@paddleocr/paddleocr-js`） | **PP-OCRv6 対応済**（→ C-7） |
| `skills/` | `paddleocr api`（ホステッドAPI）を使うエージェントスキル | 対象外 |

---

## 2. 付録B「実装時要確認リスト」の解消状況

### C-1: PP-StructureV3 サービング応答スキーマ → 【docsで確定・fixture固定は要実測】

`POST /layout-parsing`（`docs/version3.x/pipeline_usage/PP-StructureV3.en.md` §サービング）:

- リクエスト（camelCase）: `file`(Base64/URL), `fileType`(0=PDF, 1=画像), `useDocOrientationClassify`, `useDocUnwarping`, `useSealRecognition`, `useTableRecognition`, `useFormulaRecognition`, `useChartRecognition`, `useRegionDetection`, `textDetLimitSideLen`, `textRecScoreThresh`, `visualize`, `prettifyMarkdown`, `returnMarkdownImages`, `outputFormats`(["docx"]) 等。設計書 §5.3.1 の想定と一致。
- レスポンス: `layoutParsingResults[]` の各要素 =
  - `prunedResult`: predict 結果の `res` 相当
    - `parsing_res_list[]`: `{block_bbox, block_label, block_content, block_id, block_order}`（読み順ソート済み）
    - `overall_ocr_res`: `{rec_texts[], rec_scores[], rec_polys[(4,2)], dt_polys, textline_orientation_angles, ...}` ※**rec_boxes（軸平行）は無し** → §5.3.3 の rec_polys→bbox 変換は必須
    - `table_res_list[]`: `{cell_box_list[], pred_html, table_ocr_pred{rec_texts, rec_scores, rec_polys, rec_boxes}}`
    - `doc_preprocessor_res`: `{angle, ...}`（画像そのものは含まれない）
  - `markdown`: `{text, images, isStart, isEnd}`（ページ結合は isStart/isEnd を利用。Python なら `concatenate_markdown_pages()`）
  - `outputImages`（可視化画像＝オーバーレイ入り）, `inputImage`（入力画像）
- **単文字座標は layout-parsing 応答に含まれない** → DD-02 のハイブリッド（低確信スパンの crop を ocr-svc へ再問合せ）が正式に必要。

### C-2: PP-StructureV3 の OCR サブモデル指定 → 【確定・ただしリスク1件】

- 指定パラメータ: `text_detection_model_name` / `text_recognition_model_name`（Python）。この2つは `SubPipelines.GeneralOCR.SubModules.TextDetection/TextRecognition.model_name` と **`SubPipelines.TableRecognition.SubPipelines.GeneralOCR...` の両方に伝播**する（`pp_structurev3.py:368-487`）。表内 OCR も同時に v6 化される。
- サービングではリクエスト単位のモデル指定不可 → **パイプライン設定 YAML（`server/pipeline_config.yaml`）で固定**する。DD-03 の「明示固定」はこの方式。
- ⚠️ リスク: `PPStructureV3` の `ocr_version` 引数は `["PP-OCRv3","PP-OCRv4","PP-OCRv5"]` のみ（`pp_structurev3.py:28`）。**v6 は公式マッピング外**であり、明示モデル名指定でのみ組める。StructureV3 の既定 OCR は PP-OCRv5_server 系。v6_medium_det/rec を StructureV3 に組み込む構成は公式検証マトリクス外の可能性があるため、**ゴールデンセット回帰（§14.2）を通すまで本番昇格しない**こと。

### C-3: 単文字座標オプション → 【半確定・実測1点残】

- Python API: `PaddleOCR(..., return_word_box=True)` / `predict(..., return_word_box=True)`（`ocr.py:91,152,205`）。設定パスは `SubModules.TextRecognition.return_word_box`。
- **サービング `POST /ocr` のリクエストボディに returnWordBox は存在しない**（`OCR.en.md` サービング節）。→ ocr-svc は次のいずれか:
  - (a) サーバ側 pipeline config YAML に `SubModules.TextRecognition.return_word_box: True` を書き常時有効化（推奨・公式イメージ無改造）
  - (b) 薄い FastAPI ラッパーを自作し Python API を直接呼ぶ（リクエスト単位制御が要る場合）
- 単語座標の**応答フィールド名は docs 未記載** → 設計書の方針どおり recorded fixture で `packages/paddle_client/schema` に固定する（実測必須）。
- `/ocr` 応答には `rec_boxes (n,4) [xmin,ymin,xmax,ymax]` が含まれる（OCR 単体は軸平行 bbox 変換不要）。前処理可視化は `docPreprocessingImage` フィールド。

### C-4: PaddleOCR-VL-1.6 実測 → 【未解消（要ベンチ）、構成材料は確定】

- `PaddleOCR-VL-1.6` パイプライン名が存在（`paddleocr_vl.py:96`。1.5 と切替可、設計書「差替え容易」と整合）。
- VLRecognition は genai バックエンド（`vllm-server` / `sglang-server` / `fastdeploy-server` 等）へ `vl_rec_server_url` で接続する構造 → GPU プール分離（§2.3）と自然に適合。
- `deploy/paddleocr_vl_docker/` の compose（gateway + pipeline + genai）を vl-svc の雛形にできる。サービングは structure-svc と同じ `POST /layout-parsing`（+`/restructure-pages`）→ **paddle_client を共通化可能**。
- A10G スループット・GPU メモリは要実測（変更なし）。

### C-6: 法人番号チェックディジット → 【コード外・変更なし】国税庁仕様でユニットテスト作成。

### C-7: PaddleOCR.js の PP-OCRv6 対応 → 【対応済みと確認・採用検討可】

- `paddleocr-js/packages/core`: `ocrVersion: "PP-OCRv6"` で **PP-OCRv6_small** det/rec が組込み。tiny は明示モデル名指定。
- 日本語は small 以上（tiny は日本語非対応）→ クライアント側プレビューを採用する場合は small 固定。

### C-8: Redis Streams→SQS → 【PaddleOCR 範囲外・変更なし】

---

## 3. 設計判断（DD）との突合

| DD | 判定 | 根拠 |
|---|---|---|
| DD-01 ビューア画像＝前処理後画像 | ⚠️ **ギャップあり** | `/layout-parsing` 応答に「クリーンな前処理後画像」が無い（`outputImages` はオーバーレイ入り可視化、`inputImage` は入力原画、`doc_preprocessor_res` は angle 等のみ）。`docPreprocessingImage` は OCR パイプラインの応答フィールドで layout-parsing には無い。→ §4 対応案参照 |
| DD-02 主経路は StructureV3 単一呼出し＋crop 再問合せ | ✅ 妥当 | 単文字座標が layout-parsing に無いことをコードで確認。ハイブリッド必須 |
| DD-03 サブモデル明示固定 | ✅ 実現可 | C-2 の通り。ただし v6×StructureV3 は要回帰検証 |
| DD-08 tiny×日本語禁止 | ✅ 裏取り完了 | 公式 docs「tiny は 49 言語、日本語を除く」（`PP-OCRv6.en.md`）。paddleocr 側に検証は無い（明示指定なら通ってしまう）→ **自前起動時バリデーション必須**という設計は正しい |
| DD-09 VL 由来は自動確定禁止 | ✅ 変更なし | VL は genai ベースで幻覚リスク構造は設計想定通り |
| §2.6/§13.2 性能仮置き | ✅ 公式ベンチと一致 | OpenVINO CPU 1.40s/頁、A100 0.29s/頁、v6_medium は v5_server 比 GPU 2.37× 等（`PP-OCRv6.en.md`） |

---

## 4. DD-01 ギャップへの対応案（要チーム判断）

前処理後画像（座標系の正）をどう確定させるか:

1. **【推奨】前処理を ingest-svc 側へ移す**: structure-svc は `useDocOrientationClassify=false / useDocUnwarping=false` で呼び、ingest-svc がページ画像生成時に向き補正（必要なら doc_preprocessor パイプライン単体呼出し）を行い、その PNG を「前処理後画像」として保存 → OCR 座標系と表示座標系が構造的に一致し、DD-01 の意図を最も確実に満たす。
2. angle からの決定論再現: `doc_preprocessor_res.angle` で自前回転。unwarping 使用時（スマホ撮影）は再現不可のため不完全。
3. サービング拡張: 公式イメージに前処理後画像を応答へ含めるパッチ。保守コスト増。

※ 案1の場合、スマホ撮影系（unwarping が必要な帳票）は unwarping を ingest 側で実施するか、当該ソースのみ品質ゲート閾値を下げて VL/HITL に寄せる運用とする。

---

## 5. サービング構成の確定事項

- 形態: 基本サービング（`paddlex --serve --pipeline <config>`, :8080）と高安定性サービング（PaddleX SDK/Docker, `server/pipeline_config.yaml`）は**同一 REST 契約**。設計書通り高安定性版を採用し、設定 YAML で以下を固定:
  - structure-svc: `text_detection_model_name: PP-OCRv6_medium_det` / `text_recognition_model_name: PP-OCRv6_medium_rec`、`use_formula_recognition: False`、`use_chart_recognition: False`、`Serving.visualize: False`（ペイロード削減）
  - ocr-svc: OCR パイプライン、`return_word_box: True` 固定、（照合用 small_rec は別デプロイ or 別 config）
  - vl-svc: `deploy/paddleocr_vl_docker` ベース、`pipeline_name: PaddleOCR-VL-1.6`
- `Serving.return_urls`（Base64→事前署名URL化）は **BOS（百度クラウド）のみ対応** → AWS S3 では使えない。**Base64 インラインで受けて orchestrator/ingest 側で S3 保存**する（visualize:false なら応答は実用サイズ）。
- ページ制限: 既定で PDF/TIFF 10 ページまで（`Serving.extra.max_num_input_imgs`）。ただし設計は ingest でページ分割→1ページずつ送信（並列・リトライ単位）なので既定のままで実害なし。
- 既知バグ対策: `paddleocr/_pipelines/_patch_layout_parsing.py`（unwarp 後の大座標での overflow 修正, issue #17503）が本体に同梱済み。**サービング Docker 内の paddleocr/paddlex バージョンがこの修正を含むか確認**すること。

---

## 6. 実装マッピング（設計書 → PaddleOCR API）

| 設計書コンポーネント | PaddleOCR 側 | 備考 |
|---|---|---|
| structure-svc（§2.1, §5.3） | PP-StructureV3 サービング `POST /layout-parsing` | モデルは config 固定。表は `pred_html`+`cell_box_list`+`table_ocr_pred` でセル⇔span 対応付けの材料あり（対応付け自体は自前実装） |
| ocr-svc crop 再認識（§5.3.2） | OCR サービング `POST /ocr`（config で `return_word_box: True`） | two-model agreement は small_rec 併載デプロイで実現 |
| vl-svc（§5.4） | PaddleOCR-VL-1.6 サービング（`deploy/paddleocr_vl_docker` 流用） | `POST /layout-parsing` 共通クライアント可 |
| 前処理（§5.2） | doc_preprocessor（orientation/unwarping）※配置は §4 対応案に依存 | |
| KIE（§5.5） | PaddleOCR 外（LLM 直呼び）。PP-ChatOCRv4 は `pp_chatocrv4_doc.py`（visual_predict/build_vector/mllm_pred/chat）としてベンチ比較用に利用可 | ChatOCRv4 の chat_bot は ERNIE 既定 → 比較時は config 差替え |
| `packages/paddle_client`（§15） | 本報告 §2 のスキーマを Pydantic 化 + recorded fixture 契約テスト | `api_sdk/typescript` の poller/型は参考 |
| クライアント側プレビュー（C-7） | `@paddleocr/paddleocr-js`（PP-OCRv6_small, 日本語可） | 採用は任意（MVP 外） |

---

## 7. 設計書側へのフィードバック（改訂候補）

1. **§5.3.1 注記の更新**: 単文字座標は layout-parsing 応答に含まれない（確認済み）。「含まれない場合のみ」→「含まれないため」に確定できる。
2. **DD-01 の実現方式**: 前処理後画像はサービング応答から取得できないため、§4 の対応案（推奨: ingest 側前処理）を DD-01 に追記。
3. **DD-03 に注記追加**: `ocr_version` パラメータは StructureV3 では v5 まで。v6 は明示モデル名指定のみで、公式検証マトリクス外の組合せである旨（再検討条件: 公式が v6 対応の ocr_version を追加した時点で解消）。
4. **C-7 クローズ**: PaddleOCR.js は PP-OCRv6 対応済み（日本語= small）。
5. **画面設計書のギャップ**: 詳細設計 §16.1 が参照する SCR-07（ワークフローノードエディタ）が画面設計書 v1.0 HTML に存在しない。画面設計書 v1.1 での追補が必要（MVP 後段のため優先度は中）。

---

## 8. 推奨する実装着手順序

1. `packages/paddle_client`: 本報告 §2 のスキーマで型付きクライアント + docker 起動した実サービングに対する recorded fixture 契約テスト（C-1/C-3 の残実測をここで固定）
2. `inference/`: structure/ocr/vl の pipeline config YAML + compose（`deploy/paddleocr_vl_docker` 流用）
3. `services/ingest`: ページ分割 + 前処理（§4 の判断確定後）
4. `services/orchestrator`: LangGraph 抽出グラフ骨組み（State は設計書 §4.2 のまま使用可。span 構築は rec_polys→bbox 変換 + block 順読み順）
5. 正規化器・バリデータ（§5.6/§5.7.3）は PaddleOCR 非依存のため並行着手可

（以上）
