# §16 エージェントワークフロー（自動化層）実現可能性調査 v0.1 — 技術検証報告書

## TL;DR
- **結論：ワークフロー実行エンジンは「借りずに自前実装（既存のLangGraph上に構築）」、D&Dエディタは「React Flow（MIT）を借りる」。** この判断はQ1（ライセンス）で確定する。Dify・n8n・WindmillはいずれもマルチテナントSaaSへの組込みをライセンス原文で禁止/有償化しており、C1（1インスタンスに複数顧客）と衝突するため除外。ライセンス的に残るTemporal/Prefect/Argo/Kestraは常駐サーバ前提でC3（使う時だけup、月4回×8h≒$28/月）と衝突する。すでに実AWS稼働中のLangGraph 1.2.9 + PostgresSaver 3.1.0が追加コストゼロでワークフロー層のdurable execution要件を満たすため、エンジンを借りる動機が消える。
- **Q6（常駐）の結論：source.folder_watchの常駐watcherは採用せず、S3イベント通知→EventBridge→ECS RunTaskのイベント駆動に置き換える。** ただしRDS停止からの復帰（minutes to hours）＋Fargate起動（30-45秒）で「即時処理」は名乗れない。自動トリガーは常時稼働環境限定の有料オプションとして切り出すのが妥当。
- **Q4の落とし穴（DD-11の生命線）：LangGraphのPostgresSaverはテーブル名（checkpoints等）が固定・スキーマ非修飾で、Python版にはschema=引数が存在しない。** 抽出グラフとcheckpointsテーブルを共有してしまうため、レイヤ分離には別スキーマ（search_path固定）または別DBが必須。ここはローカル実測が要る最重要項目。

---

## Key Findings

### Q1（最優先）ライセンス — 候補×可否×根拠条文

| 候補 | ライセンス | マルチテナントSaaS組込み | 根拠条文（一次情報） |
|---|---|---|---|
| **Dify** | Apache 2.0 + 追加条項 | **NG（明示禁止）** | `github.com/langgenius/dify/blob/main/LICENSE`原文「Multi-tenant service: Unless explicitly authorized by Dify in writing, you may not use the Dify source code to operate a multi-tenant environment.」1テナント=1ワークスペースと定義 |
| **n8n** | Sustainable Use License | **NG（要商用契約）** | `docs.n8n.io/sustainable-use-license/`「not making n8n available to your customers for them to connect their accounts and build workflows」内部業務目的に限定。SaaS組込みはEmbed License（個別・商用・高額）が必要 |
| **Windmill** | AGPLv3 + Enterprise | **NG（実質不可）** | `github.com/windmill-labs/windmill` README原文「To re-expose directly any Windmill parts to your users as a feature of your product... you must get a commercial license」。AGPLは自製品もAGPL化を要求 |
| **Flowise** | Apache 2.0（core） | 条件付きOK | `github.com/FlowiseAI/Flowise/blob/main/LICENSE.md`。coreはApache 2.0でwhite-label/multi-tenant/resale可。`enterprise/`配下（SSO/RBAC/IdentityManager）のみ商用ライセンス |
| **Temporal** | MIT | OK | `github.com/temporalio/temporal/blob/main/LICENSE`。serverもSDKもMIT、制限なし |
| **Prefect** | Apache 2.0 | OK | 制限なし |
| **Argo Workflows** | Apache 2.0 | OK | CNCF/財団所有で単一商用主体に縛られない |
| **Kestra** | Apache 2.0（core） | OK | coreはApache 2.0。EE機能（SSO/RBAC/マルチテナント）は有償 |

**結論：Dify・n8n・WindmillはライセンスでNG。** これは伝聞ではなくライセンス原文（GitHub該当ファイル）で確認済み。打ち切り基準に従い、これら3つのエンジン組込みに関するQ2/Q3(1)/Q7の当該部分は不要になる。ライセンス的にOKな候補はFlowise・Temporal・Prefect・Argo・Kestra。ただしこれらは次のQ2/Q6で運用制約に照らして再評価する。

### Q2 マルチテナント分離（RLS）に載るか

ライセンスを通過した候補のうち、Temporal/Prefect/Argo/Kestraは**自前のDBスキーマとテナント/ワークスペース概念を持つワークフロー基盤**であり、我々のRLS（`tenant_id = current_setting('app.tenant_id', true)`、ENABLE+FORCE、非所有ロール接続）モデルに素直には載らない。これらは自製の接続プールで独自の接続ロールを使うため、「エンジンが所有者ロールで接続してRLSが所有者バイパスで無効化される」という我々が実測で発見して修正した穴と同じ構造的リスクを抱える。Web調査では2テナント分離の実行検証は不可能であり、この点は本来ローカル実測が必須。

ただし決定的なのは、**Q6の運用制約でTemporal系が先に脱落する**ため、Q2のRLS適合性を深掘りするまでもなく候補から外れることである（下記Q6）。Windmillは「production multi-tenant grade secure（nsjail + ワークスペースごとの暗号鍵）」を謳うが、ライセンスNGで検討対象外。

### Q6（運用の根幹）常駐と「使う時だけup」の両立

- **Fargate常駐watcherのコスト：** 0.5 vCPU + 1GBを24/7で**約$17.87/月**（AWS公式Fargate料金ページ us-east-1 Linux/x86：$0.04048/vCPU時 + $0.004445/GB時。LeanOps検証値でも「0.5 vCPU / 1GB costs $17.87/month」と一致）。単体は安いが、常駐させると環境全体を落とせなくなり、DB・ALB・NAT等を含めた常時稼働で月$606相当に戻る（利用者が「完全にコストオーバー」と明言）。
- **案2（S3→EventBridge→ECS RunTask）：** S3イベント通知はFargateを直接起動できず、EventBridge経由が必須（AWS re:Post/公式ブログ）。Fargateタスクの起動は「It is normal for new AWS Fargate tasks to take 30-45 seconds or longer to start」（AWS re:Post公式回答）。イベント駆動なので月4回×8hパターンなら常駐watcherの追加コストは不要。
- **RDS側の制約：** 停止済みRDSインスタンスの起動は「Starting a DB instance requires instance recovery and can take from minutes to hours」（AWS公式ドキュメント USER_StopInstance.html）。さらにRDSは**7日連続停止後に自動起動**する（同公式：「If you don't manually start your DB instance after it is stopped for seven consecutive days, RDS automatically starts your DB instance for you」）。したがって「落としっぱなし」運用はイベント RDS-EVENT-0154 起点のStep Functions/Lambdaでの再停止自動化が必要（AWS Architecture Blog "Field Notes"）。
- **Aurora Serverless v2 scale-to-zero：** 最小0 ACUで自動一時停止でき、復帰は「It can take up to 15 seconds for the database to resume」（AWS公式発表2024-11-20 / Database Blog。対応版はAurora PostgreSQL 13.15+/14.12+/15.7+/16.3+）。ただし24時間超の一時停止後は「Deep Sleep」で復帰30秒以上（AWS Hero記事のコミュニティ検証。AWS公式ではない点に注意）。いずれにせよ「即時処理」を名乗るのは困難。
- **各案の月額（月4回×8h利用パターン）：**
  - 案1 常駐watcher：watcher単体は月$9-18程度だが、環境を落とせなくなり**実質$606/月**に戻る
  - 案2 イベント駆動（S3→EventBridge→RunTask）：追加常駐コストほぼゼロ、起動レイテンシ30-45秒＋DB復帰
  - 案3 EventBridge Scheduler（cron相当）：ほぼゼロ、定期起動のみ
  - 案4 manual/schedule限定：ゼロ

**結論：常駐は本環境の運用と根本的に相性が悪い。** source.folder_watch（S3を60秒間隔で常駐監視）は設計から外す。自動トリガーはS3イベント駆動（案2）または定期起動（案3）に置き換え、「即時処理」は約束しない。**「自動トリガーは常時稼働環境でのみ提供する有料オプション」というプロダクト側の切り分けは成立する。**

この制約は、常駐サーバを要する既製エンジンすべて（Temporal Server + DB、Prefect Server、Argo=K8s常駐、Kestra常駐）を脱落させる。Temporal Cloud（マネージド）はEssentials「the greater of $100/month or 5% of your Temporal Cloud consumption」（1M Actions・1GB Active Storage・40GB Retained Storage含む、公式 docs.temporal.io/cloud/pricing）だが、これも常時接続前提でC3と衝突し、かつAction課金モデルで我々のコスト前提と整合しない。

### Q4（重要）永続的な実行エンジン — LangGraphで足りるか

（本セクションはサブエージェントによる一次情報調査で裏付け。パッケージは`langgraph-checkpoint-postgres` 3.1.0、`PostgresSaver`/`AsyncPostgresSaver`、psycopg3依存。）

1. **数日単位の待機：** 公式ドキュメント（docs.langchain.com/oss/python/langgraph/durable-execution）は「even after a significant delay (e.g., a week later)」resumeできると明記。`interrupt()`で中断し`PostgresSaver`に永続化した状態は、常駐プロセスなしで何日でも保持され、同一`thread_id`で再invokeすればresume可能（「No Python process needs to stay alive」）。**PostgresSaverには組込みTTLが存在しない**（公式サポート記事「No TTL is configured, allowing old checkpoints to accumulate」）ため、放置しても勝手に消えない（逆に自前でクリーンアップcronが必要）。C5（数時間〜数日のHITL待ち）を満たす。
2. **動的グラフ構築：** `StateGraph`はビルダーで、JSON/DB定義から実行時に`.add_node`/`.add_edge`して`.compile()`することは可能かつ実務的（公式：StateGraphは実行前に`.compile()`必須）。ただし**compile()の公式レイテンシ・ベンチマークは存在しない**。compileは純粋なメモリ内処理（I/Oなし、検証+Pregel組立）だが、高スループット時はgraph_jsonのハッシュをキーにcompile済みグラフをキャッシュすべき（本評価はアーキテクチャ＋コミュニティ報告からの推測）。
3. **checkpointerのテーブル分離（DD-11の最重要点）：** PostgresSaverは`checkpoints`/`checkpoint_writes`/`checkpoint_blobs`/`checkpoint_migrations`の**固定・非修飾テーブル名**を作る（ソース：`libs/checkpoint-postgres/langgraph/checkpoint/postgres/base.py`の`MIGRATIONS`リスト）。**Python版には`schema=`引数が存在しない**（JS版のみPR #838で対応）。GitHub Issue **#7345**（2026-03-30起票「Configurable PostgreSQL schema for langgraph-checkpoint-postgres」）がパリティ要求として提起中で、起票者自身が「Currently we can achieve it through search_path, but it needs careful design of handling connection through pool which can lead to error and can cause data leakage」と警告。**したがって抽出グラフとワークフローグラフのcheckpointsテーブル衝突を防ぐには、別スキーマ（接続のsearch_pathを確実に固定）または別DB/別接続文字列が必須。thread_id名前空間だけでは同一テーブル内の行分離にしかならずテーブル分離にならない。** これはローカル実測が要る。
4. **代替durable execution：** Temporalは常駐サーバ+DB（Cassandra/PostgreSQL + Elasticsearch）が必須でC3と衝突。Prefectもサーバ常駐（かつワーカークラッシュ時のin-flightタスク喪失リスク）。RestateもサーバプロセスがC3と衝突。DBOS（Postgres上のライブラリ型durable execution、LangGraphのPostgresSaverと併用可）は常駐サーバ不要な数少ない選択肢だが、既存のLangGraphで足りるなら追加不要。
5. **resume時の副作用（設計上の注意）：** 公式durable-executionドキュメント「Nodes after the checkpoint re-execute, including any LLM calls, API requests, or interrupts — which are always re-triggered during replay」。resumeはノードを先頭から再実行するため、interrupt前の副作用（DB書込み・課金APIなど）は**冪等**にする必要がある。また「PostgresSaverを直接使う場合、デフォルトでrun全体の間DB接続を保持する」ため、長いHITL待ちの間は接続を保持せず、resume時に再取得する設計が望ましい。

**結論：LangGraphで足りる。** 追加コストゼロ、C2/C3/C4/C5すべてに適合。設計書§6.2の「1メッセージ=1ノードの継続渡し自前実装」（リトライ・冪等・並列・タイムアウトを全部自作）は、LangGraphのdurable execution + interruptで置き換えられ、その分の工数が丸ごと減る。ただし冪等性は依然として設計者責任。

### Q5 HITLの待機を既存事例はどう扱っているか
- **LangGraph：** `interrupt()`プリミティブ + checkpointerで人間タスクを永続化。プロセスが落ちても状態はPostgresに残りresume可能。待機中にワーカーを占有しない（状態はDBにdormant）。ただし**LangGraph自身にはスケジューラ/watchdogがなく**、「waiting状態のrunを検出して再開する外部の仕組み」は自前で作る必要がある。我々の`workflow_runs.status='waiting_hitl'`＋`document.confirmed`イベントで再開する自前実装は、まさにこの外部スケジューラに相当し、概念的に一致するので乗れる。
- **Temporal：** Signalで人間承認を待つ。durableかつワーカーを占有しないが常駐サーバ必須（C3と衝突）。
- **Dify/n8n：** human-input/approvalノードあり（DifyはDBにrunを保持しpaused、n8nはWait/Formノード）。ただし両者ともライセンスNGで採用外。

### Q3 D&Dエディタの実現手段
1. **Dify/n8nのエディタ切り出し：** ライセンスNGのため不可。内部：n8nはフロント/バック分離のTypeScript製、DifyはApache 2.0だが`web/`配下にLOGO/著作権表示の改変禁止条項。Flowiseは明示的に**React Flow + LangChain.js**で構築。
2. **React Flow（xyflow）：** MITライセンス。商用製品での利用にサブスク不要（公式Discussion #3397「You don't need a subscription to use React Flow within a commercial product」）。Pro（有償サブスク）はexamples/templatesとattribution削除権のみで、core機能はMITで完結。Next.js（App Router含む）対応。ただしノードはReact DOMコンポーネントで描画するため、大規模グラフ（開発者survey上5,000+ノード）ではmemo化必須という既知の弱点（本件のノード50+規模では問題にならない見込みだが要検証）。
3. **ノード設定フォーム（最重要）：** n8nはノードごとに`INodeTypeDescription.properties[]`（displayName/name/type/options/typeOptions等）を宣言し、そこからUIを生成する方式で、純粋なJSON Schemaではなく独自プロパティ記述。**我々の推奨経路：pydanticモデル→`model_json_schema()`→react-jsonschema-form（rjsf）でフォーム自動生成が成立する。** pydanticは公式にJSON Schema生成をサポート（`model_json_schema()`）、rjsfはJSON Schemaからフォームを自動生成する成熟ライブラリ。これにより13種×手書きフォーム（UI工数がエンジン本体を超えるリスク）を回避できる。ただしrjsfは`anyOf`/`oneOf`/`dependencies`に制約があり、ノード種別による複雑な条件付き表示はカスタムwidget/fieldが要る。
4. **大規模グラフ描画性能：** React FlowはDOM描画のためCanvas/WebGL系（Cytoscape.js等）より早く性能上限に当たる。ノード50+程度なら実用範囲だが、custom nodeのmemo化を前提とすべき。

### Q7 DD-12（顧客DBへの書込み）の実現手段
- 既製エンジンを採用しないため、DB書込みノードは自前実装。任意SQL不可・allowed_tables限定・行数上限はDD-12通りに実装できる（既製エンジンの拡張点/フックの有無を気にする必要がない自前実装の利点）。
- **接続情報の保持：** n8nは資格情報を`N8N_ENCRYPTION_KEY`でDB内に暗号化保存（外部Secrets参照も可能だがデフォルトはDB内暗号化）。我々のSecrets Manager参照方式（`secret_ref`のみDB保持）の方が要件に厳格に適合し、既製エンジンに合わせる必要がない。
- **識別子のSQLインジェクション防止：** テーブル名・列名は識別子でありプレースホルダでバインドできないため、`psycopg.sql.Identifier`（psycopg3）でクォート/エスケープするのがベストプラクティス。`allowed_tables`ホワイトリスト照合と併用する（Identifierだけに頼らず、まず許可リストで弾く）。

### Q8 graph_json設計の参考
- **条件分岐の式（最重要）：** 実運用エンジンの実際の文法：
  - **n8n IFノード：** 単一ノード内でAND**または**ORを選べるが**混在不可**。UIでドロップダウンを変えると全条件のオペレータが一括で変わる。(A AND B) OR C は複数IFノードのチェーンかCodeノードが必要。3分岐以上はSwitchノード。data type別の比較演算子（string/number/boolean/date/array/object）。
  - **Dify IF/ELSEノード：** IF/ELIF（複数可）/ELSE。単一条件内でAND/OR複合をサポート。演算子はcontains/not contains/starts with/ends with/is/is not等、変数型依存。
  - **実利用者の条件式の傾向：** 実運用エンジンは複合条件（AND/OR）を標準機能として提供しており、n8nですら単一ノードでAND/ORのいずれかは使える。DifyのDiscussion #4223では複数「else if」分岐が明示的に要望されている。**我々のMVP限定文法（`<operand> <op> <literal>`のみ、and/or無し、必要なら分岐ノードを直列）は、単項比較は賄えるが、実利用者がand/orを求める場面は現実に存在する。** 分岐ノード直列でORは表現できるがANDの表現が煩雑になりやすい。MVP後に「単一ノード内複数条件（AND/OR、混在なし）」を追加できる余地を設計に残すべき。
- **変数参照記法：** n8nは`{{ $json.field }}`／`{{ $('Node Name').item.json.field }}`、Difyは`{{#node_id.field#}}`。我々の`{{node.output.field}}`は実績ある設計と整合し妥当。
- **版管理／実行中runの保護：** n8n/Difyとも定義をJSON/YAMLで保持し、実行中runが定義変更の影響を受けない版スナップショットを持つ。我々のworkflow_runsに定義スナップショットを紐付ける設計は正当。

---

## Details
主要な数値・条文はKey Findings各節に一次情報付きで集約した。要点の再掲：
- ライセンスNG3件（Dify/n8n/Windmill）はいずれもGitHub上のLICENSE/README原文で確認。
- ライセンスOKだが常駐必須で脱落する4件（Temporal/Prefect/Argo/Kestra）はC3で除外。
- LangGraphのdurable executionは公式ドキュメントで数日単位の待機resumeを保証、TTLなし。
- PostgresSaverのテーブル固定・スキーマ非修飾はソースコード＋Issue #7345で確認、DD-11遵守の設計上の要注意点。

---

## Recommendations

**「作るか、借りるか。借りるなら何を、どこまで」への回答：**
> **実行エンジンは作る（既存LangGraph 1.2.9 + PostgresSaver 3.1.0を自動化層のdurable executionエンジンとして流用）。借りるのはD&DエディタのUI部品のみ＝React Flow（MIT）＋pydantic→JSON Schema→rjsfのフォーム自動生成ライブラリ群まで。ワークフロー実行エンジンそのものは借りない。** この判断はQ1（ライセンス）とQ6（常駐と運用）で確定し、Q4（LangGraphで足りる）が裏付ける。

**段階的な次アクション：**
1. **【即断可】エンジンは自前実装（LangGraph上）に確定。** Q1でDify/n8n/Windmillがライセンス除外、Q6で常駐系（Temporal/Prefect/Argo/Kestra）が運用除外されるため、既製エンジン組込みの選択肢は実質消滅。既存資産を流用し、設計書§6.2の継続渡し自前実装（リトライ・冪等・並列・タイムアウト）を丸ごと不要にする（工数が1桁減る狙い）。
2. **【即断可】D&DエディタはReact Flow（MIT）を採用。** ノード設定フォームはpydantic→JSON Schema→rjsfの自動生成経路をPoCで検証し、13種手書きを回避する。
3. **【設計変更】source.folder_watch常駐を削除。** トリガーはmanual/schedule + S3イベント駆動（EventBridge→ECS RunTask）に縮小。「即時処理」は約束せず、自動トリガーは常時稼働環境の有料オプションとして切り出す。
4. **【実測必須・実装着手前提】** 下記「ローカル実測が必要な項目」を先に消化。特にcheckpointerのスキーマ分離とRLS×LangGraph接続ロールの検証は、失敗すると「テナント分離が壊れる」「レイヤ混在でDD-11違反」という最悪結果に直結する。

**判断を変える閾値：**
- 顧客が「即時処理（数十秒以内）」を契約要件として求めるなら → 常時稼働環境（有料オプション）が必須になり、コスト前提が$28/月から$606/月級に変わる。イベント駆動案では起動レイテンシ（Fargate 30-45秒＋RDS復帰 minutes〜）を吸収できない。
- ワークフローの同時実行数・スループットが跳ね上がるなら → LangGraphのcompile()キャッシュ戦略とcheckpoint書込みレイテンシ（コミュニティ報告で20-50ms/write）が律速になり、再評価が要る。
- and/or複合条件が顧客要望で頻出するなら → MVP限定文法を単一ノード内複数条件（AND/OR、混在なし）に拡張する（n8n方式が参考）。
- Issue #7345がマージされPython版PostgresSaverに`schema=`が入るなら → スキーマ分離のsearch_path手運用リスクが解消され、実装が簡潔になる。

---

## Caveats
- **Web調査では実機実測（2テナント分離の実行検証、数日放置resume、PoCスクリーンショット等）が不可能。** ライセンス原文・GitHubソース・公式ドキュメントで代替したが、下記は必ずローカルで検証すること。
- ドキュメント記述と実運用は食い違う。特にLangGraphのcompile()レイテンシは公式ベンチマークが存在せず、本報告の「モデレート」評価はアーキテクチャからの推測。
- RDS/Aurora復帰時間・Fargate起動時間は環境・リージョン・イメージサイズで変動する。Aurora「Deep Sleep」30秒以上はAWSコミュニティ（Hero記事）由来でAWS公式記述ではない。
- **ライセンス解釈は最終的に法務確認が必要。** 特にn8n Embed License、Dify商用ライセンスの価格体系（テナント数課金か収益シェアか）は原文で確認できず「要問い合わせ」。Windmillの「re-expose to your users」の解釈境界も、境界的な使い方をするなら法務確認を推奨。自己判断で白黒つけない。

### ローカルでの実測検証が必要な項目リスト
1. **checkpointerのスキーマ分離：** PostgresSaverを別スキーマ（search_path固定 or 別DB）に隔離し、抽出グラフの`checkpoints`テーブルと衝突しないことを2グラフ同時稼働で実測（DD-11の生命線。Issue #7345のpool経由データ漏洩警告を踏まえ、接続初期化で確実にsearch_pathを固定できるか検証）。
2. **RLS × LangGraph接続ロール：** LangGraphのPostgresSaverが非所有ロールで接続し、RLSポリシー（`tenant_id = current_setting('app.tenant_id')`）が効くことを2テナントで分離実測（所有者バイパスの穴の再発防止）。PostgresSaverが接続時にどのロールを使い、それが設定可能かを確認。
3. **数日放置resume：** `interrupt()`→PostgresSaver永続化→3日放置→resumeが成功し、副作用が冪等に扱われることを実測。
4. **動的StateGraph構築とcompile()レイテンシ：** テナント別graph_jsonからの実行時構築＋compile()の実測レイテンシ、キャッシュ有無での差。
5. **S3→EventBridge→ECS RunTask起動レイテンシ：** ファイル配置から処理開始までのend-to-end実測（RDS/Aurora復帰込み）。
6. **pydantic→JSON Schema→rjsfフォーム自動生成：** 13種ノードconfigすべてでフォームが正しく生成・検証されるか、anyOf/oneOf制約に当たらないかのPoC。
7. **React Flow大規模グラフ描画：** ノード50+でのcustom nodeのmemo化前後の描画性能。
8. **psycopg.sql.Identifierによる識別子安全性：** allowed_tablesホワイトリスト + Identifierクォートでテーブル名・列名インジェクションを防げるかの検証。