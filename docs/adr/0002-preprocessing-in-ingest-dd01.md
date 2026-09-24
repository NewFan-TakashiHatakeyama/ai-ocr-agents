# ADR-0002: 前処理を ingest-svc 側で行い、座標系の正を確定する（DD-01 の実現方式）

- 状態: Accepted
- 日付: 2026-07-14
- 関連: 詳細設計 DD-01 / §5.2 / §5.3、PaddleOCR適合調査報告 §4

## コンテキスト

DD-01 は「HITL ビューアにはパイプラインが返す前処理済みページ画像（docPreprocessingImage）
を座標系の正として表示する」と規定していた。

しかし PaddleOCR 適合調査（報告 §4, C-1）で、PP-StructureV3 サービングの
`POST /layout-parsing` 応答には**クリーンな前処理後画像が含まれない**ことが判明した:

- `outputImages` はレイアウト/OCR オーバーレイ入りの可視化画像
- `inputImage` は入力原画そのもの
- `doc_preprocessor_res` は `angle` 等のメタのみ（画像本体なし）
- `docPreprocessingImage` は OCR パイプライン（`/ocr`）側の応答フィールドで、
  layout-parsing には存在しない

## 決定

前処理（向き補正・必要時のアンワープ）を **ingest-svc 側に移す**。

1. ingest-svc がページ画像生成時に前処理を行い、その PNG を「前処理後画像 =
   座標系の正」として `pages/{n}.png` に保存する（pages.image_uri）。
2. structure-svc / ocr-svc は `useDocOrientationClassify=false`,
   `useDocUnwarping=false` で呼び出す（二重前処理を避ける）。
3. これにより OCR 座標系と HITL 表示座標系が構造的に一致し、DD-01 の意図
   （座標系の一致）を最も確実に満たす。

## トレードオフ / 留意点

- スマホ撮影系（アンワープが必要な帳票）は ingest 側でアンワープを実施する。
  アンワープ判定は EXIF 有無＋台形歪み簡易検知（§5.2）。
- 前処理は PaddleOCR の doc_preprocessor パイプライン（orientation/unwarping モジュール）
  を ingest-svc から単体呼び出しする実装余地を残す（MVP 初期は orientation のみでも可）。
- `pages.preproc`(JSONB) に回転角・unwarp 有無・縮小倍率を記録し、原本との対応を保持。

## 設計書への反映

反映済み: 詳細設計 v1.2（`NewFan_AI-OCRエージェント詳細設計書_v1.2.md`）で DD-01 の本文を
本 ADR の方式（ingest 側前処理）に更新済み。

## 追記（2026-09-13）: 傾き補正の最小実装（`DeskewPreprocessor`）

「軽量な自前回転」として、射影プロファイル法の傾き補正 `newfan_ingest.preprocess.DeskewPreprocessor`
を入れた（Pillow のみ、±5° を 1° → 0.25° の 2 段で探索、幅 800 px の縮小グレースケールで判定、
補正は元解像度で bicubic・余白は白・**寸法は変えない**）。有効化は `INGEST_PREPROCESS=deskew`
（既定 `none`）。gateway の手動アップロードと orchestrator-worker の自動取込（S3 / SaaS）の
両方が同じ環境変数を読む。適用した回転角（反時計回りが正）は `pages.preproc.angle`、判定の内訳
（推定角・山の高さ `gain`・適用したか）は `pages.preproc.deskew` に残る。

補正しない条件: |角| < 0.3°、または最良角の射影分散が 0° の 1.05 倍に届かない（山が無い＝白紙・
写真・罫線だけ）。画像を開けない場合は warning を出して無補正で通す（取込を止めない）。

未実装のまま: 90°/180° の向き補正（orientation）、アンワープ。方針決定の材料は
`docs/design/dd01-deskew-measurement-2026-09-13.md`（手元 30 帳票の傾き分布）。

### 追記（2026-09-24）: 測るだけのモード `deskew_measure`

`INGEST_PREPROCESS=deskew_measure` は傾きを推定して `pages.preproc.deskew`（`estimated` / `gain` /
`would_apply` / `measure_only: true`）に残すだけで、画像は回さない。sample2（幅 740 px）を 2° 傾けた
合成画像で比べると、補正なしは PaddleOCR がそのまま正しく読み、補正ありは再標本化で 2 文字を誤読した
（`docs/design/dd01-deskew-measurement-2026-09-13.md` の追記）。補正の効き目は実帳票の傾き分布しだいなので、
本番で `deskew` を検討する前に、まず `deskew_measure` で分布を測る。
