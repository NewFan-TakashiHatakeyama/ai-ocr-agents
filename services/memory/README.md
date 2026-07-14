# newfan-memory

修正メモリ・ルール（詳細設計 §5.8, DD-06 / DD-07）。

## 提供物

| モジュール | 内容 |
|---|---|
| `embedding.py` | 埋め込みキー（§5.8.2）・`Embedder` Protocol・`HashingEmbedder`（テスト/dev, 決定論・語彙類似） |
| `e5_embedder.py` | `E5Embedder`（本番, intfloat/multilingual-e5-small 384次元, ローカル推論）。embeddings extra |
| `index.py` | `VectorIndex` Protocol・`InMemoryIndex`（純Python IndexFlatIP＝cosine） |
| `faiss_index.py` | `FaissIndex`（本番, テナント別 IndexIDMap+IndexFlatIP）。faiss extra |
| `records.py` | correction_logs / tenant_memories / tenant_rules（§7.2） |
| `repository.py` | `MemoryRepository` Protocol・`InMemoryMemoryRepository` |
| `rules.py` | ルール適用エンジン（regex_replace/vocab_map、他は no-op）＋**自動検証**（§5.8.4）＋ライフサイクル |
| `rule_extract.py` | 修正ログ→draft ルール（llm-adapter, §4.6.3） |
| `service.py` | `MemoryService`: search / add / active_rules / **learn** |

## 設計方針（DD-06 / DD-07）

- **埋め込みはローカル**（e5-small、外部APIに顧客データを出さない, DD-06）。テストは HashingEmbedder。
- **ベクタ検索は FAISS**（テナント別 IndexFlatIP, DD-07）。正本は PostgreSQL、index は再構築可能な派生物。
- 埋め込み/index はいずれも Protocol で差し込み式（本番は e5/faiss、テストは Hashing/InMemory）。

## learn とルールライフサイクル（§5.8.4）

`learn()` は修正を embedding 登録し、**同種修正（tenant×doc_type×field）が min_evidence(=5) 以上**
たまったらルール抽出を起動。抽出した draft ルールを**自動検証**する:

- 「過去修正の90%以上を再現」（regex/vocab を original に適用して corrected を得られる割合）
- **かつ「確定済み正解値への誤適用0件」**（正解値を1件でも変えたら不合格）

両方満たせば `active`、さもなくば `draft` 据え置き。`active_rules()` は active のみ返し、
orchestrator の `deterministic_normalize` がテナントルールとして適用する。

## orchestrator への配線

`build_graph(memory=...)` を渡すと `memory_lookup`（active_rules + few-shot 取得）と `learn`
（確定時の修正登録＋ルール抽出）が実体化される。`deterministic_normalize` は state の
`active_rules` を値へ適用する（regex_replace/vocab_map、エラー時は skip+WARN）。

## テスト / 本番

```bash
uv run pytest services/memory services/orchestrator
```

本番は `embeddings`（sentence-transformers）+ `faiss` extra、正本は PostgreSQL（Alembic）。
Neo4j へのルール関係登録（§5.8.4）は未実装（影響範囲照会・重複検知用、フェーズ2）。
