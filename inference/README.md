# inference/ — 推論層サービング設定

PaddleOCR 自己ホストサービング（structure / ocr / vl）のパイプライン設定。
詳細設計 §2.2 / §5.3 / §5.4、PaddleOCR適合調査報告 §5 に準拠。

| サービス | 設定 | モデル固定 | 備考 |
|---|---|---|---|
| structure-svc | `structure/pipeline_config.yaml` | PP-OCRv6_medium_det/rec（通常OCR・表内OCR両方）＋ layout: PP-DocLayout_plus-L | formula/chart 無効、前処理無効（DD-01/ADR-0002） |
| ocr-svc | `ocr/pipeline_config.yaml` | PP-OCRv6_medium_det/rec | `return_word_box: True` 固定（付録C-3） |
| ocr-svc（照合） | `ocr/ocr_small_pipeline_config.yaml` | PP-OCRv6_small | two-model agreement 用（任意） |
| vl-svc | `vl/pipeline_config.yaml` | PaddleOCR-VL-1.6-0.9B ＋ layout: PP-DocLayout_plus-L | genai(vLLM) 別コンテナ、GPU専用プール（DD-09） |

### CPU 推論バックエンド実測（sample2.png / PP-OCRv6_medium + PP-DocLayout_plus-L）

本番は Fargate CPU（ADR-0003 Option A, GPU回避）のため推論バックエンドが効く。同一条件で実測:

| エンジン | init | 推論(warm) | 抽出 | 精度 |
|---|---|---|---|---|
| `paddle`（現行既定） | 5.5s | 79.7s（44〜83s と変動） | texts=94 / tables=1 | conf 0.9689 |
| **`onnxruntime`** | 4.9s | **8.8s**（8.2〜10.4s と安定） | texts=94 / tables=1 | **conf 0.9689（完全一致）** |

**約 4.6〜9× 高速化し、抽出結果・信頼度は完全一致（非劣化）**。paddle 側は測定値の変動が大きく
倍率に幅があるが方向は明確。初回のみ paddle2onnx 変換で +60s 程度かかるがキャッシュされ、
2回目以降の init は約5s（コールドスタート影響は限定的）。依存は `onnxruntime` + `paddle2onnx`
（pip 導入・クロスプラットフォーム）。

#### OpenVINO は不可（Linux コンテナ実測で否決, 2026-07）

リリースの「CPU 5.2×高速化（OpenVINO）」を本番同等の **linux/amd64 コンテナ**
（python:3.12-slim ＋ `paddlex --install hpi-cpu` → `ultra_infer 1.2.0`, `openvino=True`）で検証した結果、
**PP-OCRv6_medium_rec が OpenVINO で読み込めず落ちる**:

```
Creating model: PP-DocLayout_plus-L → Inference backend: onnxruntime   （対応表どおり）
Creating model: PP-OCRv6_medium_det → Inference backend: openvino  ✅ 成功
Creating model: PP-OCRv6_medium_rec → Inference backend: openvino  ❌ 失敗
  RuntimeError: Exception from src/inference/src/cpp/core.cpp:95:
  Check 'false' failed at src/frontends/onnx/frontend/src/frontend.cpp:164:
  FrontEnd API failed with GeneralFailure
```

det は OpenVINO で動くが **rec（認識）が ONNX フロントエンドで GeneralFailure**。
ultra-infer 1.2.0 の OpenVINO が最新 PP-OCRv6_medium_rec に追随できていない。
→ **現時点で OpenVINO は採用不可**。`onnxruntime` を使う（上表のとおり十分速く、精度も同一）。
将来 ultra-infer が更新されたら再評価する。

前提情報（調査時の事実）:
- `ultra-infer` のホイールは **linux_x86_64 のみ**（Windows 版なし。PyPI にも無く、paddlex 同梱の
  `hpip_links.html` 経由で `paddlex --install hpi-cpu`）。
- HPI 対応表（`hpi_model_info_collection.json`, cpu_x64/paddle311）では OpenVINO が使えるのは
  **OCR det/rec だけ**で、レイアウト・表構造は onnxruntime/mkldnn に落ちる:

| モデル | CPU バックエンド優先順 |
|---|---|
| PP-OCRv6_medium_det / _rec | **openvino** → paddle_mkldnn → onnxruntime → paddle |
| PP-DocLayout_plus-L | onnxruntime → paddle_mkldnn → paddle（openvino 非対応） |
| SLANet_plus / SLANeXt_wired | onnxruntime → paddle（同上） |
| RT-DETR-L_wired_table_cell_det | onnxruntime → paddle_mkldnn → paddle（同上） |

つまり「5.2×」は PP-OCRv6 単体の数字で、PP-StructureV3 全体には直接適用できない。
なお paddle 3.3.1 は HPI 対応表に無く「Paddle 3.1.1 の事前知識を使用」へフォールバックする（動作する）。

#### ⚠️ 未解決: この設定はそのままでは serve できない

`paddlex --serve --pipeline inference/structure/pipeline_config.yaml` を実コンテナで起動すると
**`ValueError: config error for doc_preprocessor_pipeline!`** で失敗する。本ファイルは
「確定手順」（下記）を前提とした**部分的な pin** であり、完全な base config とのマージが未実施のため。
`deploy/compose.yaml` はこの設定をそのまま `paddlex --serve` に渡しているので、**推論層の
サービングは一度も起動検証できていない**。本番投入前に確定手順の実行と起動確認が必須。
あわせて `--engine onnxruntime` の付与（上表の 2.5×）もここで入れる。

### PP-OCRv6 ティアと日本語対応（paddlex 3.7.2 実測）

PaddleOCR 3.7.0 のリリースノートは PP-OCRv6 を「50言語統一サポート（中国語・英語・**日本語**・
46ラテン語系）／モデル切替不要」と謳うが、**tiny ティアは日本語を読めない**。
`inference.yml` の `PostProcess.character_dict` を実測した結果:

| ティア | 総文字数 | かな | 漢字 | 日本語 | 用途 |
|---|---|---|---|---|---|
| PP-OCRv6_tiny (1.5M) | 6,904 | **0** | 6,174 | **不可** | DD-08 で日本語テナントは禁止 |
| PP-OCRv6_small (7.7M) | 18,708 | 180 | 15,565 | 可 | two-model agreement 照合用 |
| PP-OCRv6_medium (34.5M) | 18,708 | 180 | 15,565 | 可 | **本番採用**（structure/ocr 共通） |

リリースノートの「medium は PP-OCRv5_server 比で検出+4.6% / 認識+5.1%」は自前実測とも整合
（sample2.png で平均conf 0.9127→0.9689、94行中36行改善）。medium 採用の根拠。

### レイアウトモデルの選定根拠（paddlex 3.7.2 実測）

`PP-DocLayoutV3` は**存在しない**（実在: PP-DocBlockLayout / PP-DocLayout-L・M・S /
PP-DocLayoutV2 / PP-DocLayout_plus-L）。名前が新しい `PP-DocLayoutV2` は PP-StructureV3 の
相方ではなく、sample2.png 実測で**表を3分割しヘッダ行と合計/消費税行を落とす**
（列名が `col1..` になり 13行→10行、平均conf 0.9513）。`PP-DocLayout_plus-L` は
PP-StructureV3 の公式既定で 13行＋列名を正しく復元し conf 0.9689。よって plus-L を採用。

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

- DD-08: 日本語テナントで PP-OCRv6_tiny（かな0文字＝日本語不可）を弾く。下表の実測を参照。
- DD-03: OCR 検出/認識モデルの明示固定を必須化。
- 実在性: `model_name` が導入済み paddlex に実在するか（`configs/modules/<module>/*.yaml` を参照）。
  存在しない名前（例: PP-DocLayoutV3）を起動前に弾く。paddlex 未導入の CI ではスキップされるため、
  **実在性チェックは paddlex が入ったサービング環境（コンテナ entrypoint）で必ず実行すること**。

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
