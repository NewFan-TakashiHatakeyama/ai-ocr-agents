# newfan-llm-adapter

LLM/SLM 抽象と KIE/補正の実行（詳細設計 §2.1 / §4.6 / §5.5, DD-10）。

## 提供物

| モジュール | 内容 |
|---|---|
| `provider.py` | `LLMProvider` Protocol（プロバイダ切替の抽象）＋ `FakeProvider`（テスト用） |
| `anthropic_provider.py` | `AnthropicProvider`（公式 anthropic SDK, 既定 `claude-opus-4-8`）。anthropic extra |
| `adapter.py` | `LLMAdapter`: JSON 契約実行・**1回リペアリトライ**（→E3002）・トークン/コスト計測・ZDR ガード |
| `bundle.py` | `PromptBundle`: `prompts/{version}/` の YAML 読込・安全レンダリング（JSON 波括弧を温存） |
| `kie.py` | `kie_extract`: §4.6.1。**span_ids の実在をコード検証**（捏造値を除去） |
| `correct.py` | `llm_correct`: §4.6.2。**DD-10 をコードで強制**（混同文字表 or メモリ一致時のみ自動適用） |

## 設計方針

- 設計の「プロバイダ切替（litellm 抽象）」は `LLMProvider` Protocol で表現。具体実装は公式
  Anthropic SDK を使う（OpenAI 互換 shim は使わない）。他プロバイダは Protocol の追加実装で対応。
- **安全性はコード側で担保**する。LLM の自己申告（`needs_review` 等）に依存せず、
  span_ids 実在検証（KIE）と DD-10 の混同文字表チェック（補正）をアダプタ側で強制する。
- 既定モデルは `claude-opus-4-8`（構成で変更可）。ZDR 必須テナントは許可モデルをホワイトリスト化
  （`claude-fable-5` は ZDR 非対応のため既定で除外）。

## 使い方

```python
from newfan_llm_adapter import (
    AnthropicProvider, LLMAdapter, PromptBundle, default_bundle_dir, kie_extract,
)

adapter = LLMAdapter(AnthropicProvider(), model="claude-opus-4-8")
bundle = PromptBundle.load(default_bundle_dir())
result = kie_extract(adapter, bundle, spans=spans, layout_markdown=md, schema_json=schema)
```

orchestrator への配線: `build_graph(adapter=..., bundle=...)` を渡すと `kie_extract` /
`llm_correct` ノードが本アダプタで実体化される（未指定ならスタブ）。

## テスト

```bash
uv run pytest services/llm_adapter services/orchestrator
```

FakeProvider で LLM 呼出しを差し替え、KIE の span 検証・DD-10 の適用制約・JSON リペアを
API キーなしで検証する。本番実行には anthropic extra と `ANTHROPIC_API_KEY` が要る。
