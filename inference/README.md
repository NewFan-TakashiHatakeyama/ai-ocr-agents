# inference/ — 推論層サービング設定

PaddleOCR 自己ホストサービング（structure / ocr / vl）のパイプライン設定。
詳細設計 §2.2 / §5.3 / §5.4、PaddleOCR適合調査報告 §5 に準拠。

| サービス | 設定 | モデル固定 | 備考 |
|---|---|---|---|
| structure-svc | `structure/pipeline_config.yaml` | PP-OCRv6_medium_det/rec（通常OCR・表内OCR両方） | formula/chart 無効、前処理無効（DD-01/ADR-0002） |
| ocr-svc | `ocr/pipeline_config.yaml` | PP-OCRv6_medium_det/rec | `return_word_box: True` 固定（付録C-3） |
| ocr-svc（照合） | `ocr/ocr_small_pipeline_config.yaml` | PP-OCRv6_small | two-model agreement 用（任意） |
| vl-svc | `vl/pipeline_config.yaml` | PaddleOCR-VL-1.6-0.9B | genai(vLLM) 別コンテナ、GPU専用プール（DD-09） |

## 設定の確定手順（本番投入前）

各 SubModule の既定値は paddleocr バージョンに依存する。当該バージョンで完全な base
config を生成し、本ディレクトリの固定値をマージして authoritative config を作る。

```bash
# paddleocr が入ったサービング環境で
python inference/scripts/export_base_config.py structure -o structure_base.yaml
python inference/scripts/export_base_config.py ocr -o ocr_base.yaml
# 生成物に本ディレクトリの pin（モデル名・Serving・return_word_box）を反映
```

## 起動時バリデーション（DD-03 / DD-08）

```bash
uv run python inference/scripts/validate_config.py inference/structure/pipeline_config.yaml --lang ja
uv run python inference/scripts/validate_config.py inference/ocr/pipeline_config.yaml --lang ja
```

- DD-08: 日本語テナントで PP-OCRv6_tiny（日本語非対応）を弾く。
- DD-03: OCR 検出/認識モデルの明示固定を必須化。

CI とコンテナ起動 entrypoint の両方で実行すること。

## ローカル起動

```bash
docker compose -f deploy/compose.yaml up structure-svc ocr-svc
# structure: http://localhost:8081/layout-parsing
# ocr:       http://localhost:8082/ocr
```

イメージタグ・GPU 構成は環境依存（付録C-1/C-4）。起動後、代表帳票を投げて応答 JSON を
`packages/paddle_client/tests/fixtures/` に録画し、契約テストを実データで固定する。

## デバイス選択（ECS コスト最適化, ADR-0003）

本番は ECS。コスト最小トポロジ（Option A）では **structure/ocr を Fargate で動かすため
OpenVINO CPU** を使う（§2.6, `paddlex --serve ... --device cpu` 相当）。GPU が要るのは
vl-svc のみで、既定では無効化（難読ページは HITL 直行）。スループット要件から GPU が必要に
なった場合のみ、structure/ocr/vl を ECS on EC2 GPU（Option B）へ移す。CPU/GPU の切替は
サービング起動オプションで吸収し、パイプライン設定・アプリコードは共通のまま。
