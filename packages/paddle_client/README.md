# newfan-paddle-client

PaddleOCR 自己ホストサービングの型付きクライアント（詳細設計 §5.3 / 付録C-1, C-3）。

## 提供物

- `schema.py` — `/layout-parsing`（PP-StructureV3）と `/ocr`（PP-OCRv6 単体）の応答型。
  未知フィールドは `extra="allow"` で温存し、サービング差分に強い。
- `client.py` — `PaddleServingClient`。DD-01/ADR-0002 により既定で前処理オフ。
- `spans.py` — `rec_polys`→軸平行 bbox 変換、読み順ソート済み Span/LayoutBlock 構築（§5.3.3）。

## 使い方

```python
from newfan_paddle_client import PaddleServingClient, encode_image, build_spans

with PaddleServingClient("http://structure-svc:8080") as client:
    resp = client.layout_parsing(encode_image(page_png_bytes), file_type=1)
    page = resp.layout_parsing_results[0]
    spans = build_spans(page.pruned_result, page=1, start_id=0)
    markdown = page.markdown.text if page.markdown else ""
```

## 契約テスト（残実測の固定）

`tests/fixtures/*.json` は**実サービング出力の録画に置換する**プレースホルダ。

1. structure/ocr サービングを起動（`inference/` の compose）。
2. 代表帳票を投げ、応答 JSON を `tests/fixtures/` に保存。
3. 特に単語/単文字座標（`return_word_box=True` 時）の**正確なフィールド名**を確認し、
   `schema.OverallOcrRes` の候補（`rec_word_boxes` 等）を実名に確定する（付録C-3）。
4. `uv run pytest packages/paddle_client` が通ることを確認。

```bash
uv run pytest packages/paddle_client
```
