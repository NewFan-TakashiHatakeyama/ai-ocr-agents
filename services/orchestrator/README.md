# newfan-orchestrator

LangGraph 抽出グラフ（詳細設計 §4）。

## 構成

| モジュール | 内容 | 状況 |
|---|---|---|
| `confidence.py` | §5.7.2 confidence/grounding 算出（DD-09/DD-10 反映） | 実装済み・テスト有 |
| `gate.py` | §2.5 閾値ゲート → review_items | 実装済み・テスト有 |
| `nodes.py` | §4.3 全ノード。決定論ノード（confidence_score/quality_gate/confidence_gate）は実装、外部接続ノードはスタブ | 骨組み |
| `graph.py` | §4.1 グラフ組み立て（interrupt/resume は §4.4） | 骨組み（langgraph は extra） |

## グラフ形状（§4.1）

```
start → load_context → structure_ocr → quality_gate
  ├─(NG)→ vl_fallback ─┐
  └─(OK)──────────────┴→ memory_lookup → kie_extract → deterministic_normalize
   → confidence_score → llm_correct → validate → confidence_gate
      ├─(要レビュー)→ hitl_review → apply_feedback → learn → finalize → end
      └─(全確定)───────────────────────────────────→ finalize → end
```

## 実行（グラフ組み立てには langgraph が必要）

```bash
uv sync --extra graph            # services/orchestrator[graph]
uv run pytest services/orchestrator   # ノード/confidence/gate の単体テスト（langgraph 不要）
```

`build_graph(checkpointer=PostgresSaver(...))` で compile。`thread_id=run_id` で
interrupt/resume（§4.4）。再実行は resume ではなく新 Run 発行（DD-11 の境界を守る）。

## 次の実装

- `structure_ocr`: `newfan_paddle_client` でページ並列呼出し → `build_spans`/`build_layout_blocks`。
  単文字座標欠落スパンは ocr-svc へ crop 再問合せ（DD-02）。
- `kie_extract`/`llm_correct`: llm-adapter 経由。プロンプトは `prompts/2026.07-1/`。
- `deterministic_normalize`/`validate`: `newfan_normalizers`/`newfan_validators`（未着手）。
- `hitl_review`: `interrupt()` 発火。`memory_lookup`/`learn`: memory-svc 接続。
