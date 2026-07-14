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
