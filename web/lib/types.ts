// gateway-api（§6）の DTO ミラー。OpenAPI 生成に置換可能（§15）。

export type BBox = [number, number, number, number];

export type ReviewStatus = "auto" | "pending" | "corrected" | "approved";

export interface ExtractedField {
  name: string;
  label?: string | null;
  value_raw?: string | null;
  value_normalized?: string | null;
  span_ids: number[];
  page?: number | null;
  bbox?: BBox | null;
  char_boxes?: BBox[] | null;
  source_quote?: string | null;
  confidence: number;
  grounding_score: number;
  correction?: Record<string, unknown> | null;
  validation?: { checks?: string[]; passed?: boolean } | null;
  review_status: ReviewStatus;
}

export interface TableCell {
  value?: string | null;
  span_ids: number[];
  bbox?: BBox | null;
}

export interface TableResult {
  name: string;
  page?: number | null;
  structure_html?: string | null;
  rows: Record<string, TableCell>[];
  confidence?: number | null;
}

export interface ResultResponse {
  document_id: string;
  run_id: string;
  status: string;
  // スキーマレス抽出（自動発見）なら null。テンプレート化バナーの出し分けに使う
  schema_id?: string | null;
  result_version: number;
  engine_versions: Record<string, unknown>;
  fields: ExtractedField[];
  tables: TableResult[];
  review_summary: Record<string, number>;
  fallback_pages?: number[]; // VL フォールバックしたページ（§5.4）
  /** 除外領域で消した件数。0 件なら UI は出さない */
  region_stats?: RegionStats | null;
  /** この run に適用された除外領域（ページ解決済み） */
  applied_exclude_regions?: ResolvedRegion[];
  /** run.schema_id から解決した doc_type（領域編集のプリロード起点） */
  schema_doc_type?: string | null;
}

/** 最新 run の OCR span 1 件（除外領域の適用後）。bbox は前処理後画像の画素。 */
export interface RunSpanDto {
  span_id: number;
  text: string;
  bbox?: BBox | null;
}

/**
 * GET /documents/{id}/spans?page=n の応答（設計 D12）。テンプレート化画面が
 * 「枠に含まれる文字」を出し、例示値の出どころにする。行が無ければ spans は空。
 */
export interface RunSpans {
  run_id: string;
  page_no: number;
  spans: RunSpanDto[];
}

/** ページの正規寸法（前処理後 PNG 画素）。領域の正規化・逆正規化に使う。 */
export interface PageDim {
  page_no: number;
  width?: number | null;
  height?: number | null;
}

/**
 * スキーマに保存する領域。ランタイムの bbox（画素 int）とは別物で、
 * こちらは当該ページ寸法に対する正規化 [0,1] の rect。
 * page: 1始まり int / "last"（最終ページ）/ null（全ページ・除外のみ）
 */
export interface RegionRect {
  page?: number | "last" | null;
  rect: [number, number, number, number];
  label?: string | null;
  // --- 以下、読取領域のみ（除外領域では未使用）。設計 region-field-add-and-hint-v2 §2.3 ---
  /** テンプレート元の帳票でこの領域にあった span の原文（正規化しない）。帳票の値であり個人名を含み得る */
  example_value?: string | null;
  /** 領域の出どころ。ghost = AI が見つけた位置をクリックで採った / manual = 手描き */
  origin?: "ghost" | "manual" | null;
  /** ISO 8601。ヒント有効化前に引かれた領域を識別する */
  created_at?: string | null;
}

/** サーバ側でページ番号まで解決済みの領域（検証画面のオーバーレイ用）。 */
export interface ResolvedRegion {
  page_no: number;
  rect: [number, number, number, number];
  label?: string | null;
}

/** 除外領域で消した件数（検証画面のバッジの材料）。 */
export interface RegionStats {
  excluded_spans?: number;
  excluded_cells?: number;
  excluded_rows?: number;
  skipped_pages_no_dims?: number[];
  markdown_dropped_pages?: number[];
  /** 別レイアウトと判定して除外領域を適用しなかったページ（REGION_EXCLUDE_SKIP_ON_LAYOUT_MISMATCH） */
  skipped_exclude_pages?: number[];
  layout_probe_matched?: number;
  layout_probe_total?: number;
  mismatch_fields?: string[];
  layout_mismatch?: boolean;
  /** 読取領域ヒント（設計 region-field-add-and-hint-v2 §2.5・§2.6）。ヒント有効時のみ */
  hints?: RegionHintStats;
}

/** ヒントの内訳。given は渡した項目、dropped は渡す前に落とした項目と理由、
 *  outcomes は LLM が従ったか（span_ids と候補の集合演算。モデルの申告ではない）。
 *  pre_activation は評価した項目のうち、領域がヒント有効化の時点（既定 2026-09-12。
 *  サーバの環境変数 REGION_HINTS_ACTIVATED_AT で上書き可）より前に引かれたもの
 *  （created_at が無い・古い。§1.5）。判定はサーバが行い、参考表示にしか使わない。 */
export interface RegionHintStats {
  given?: string[];
  dropped?: Record<string, string>;
  truncated?: Record<string, number>;
  outcomes?: Record<string, "followed" | "partial" | "rejected" | "no_evidence" | string>;
  /** 項目名 → 例示値と候補の原文（先頭数件、各 40 字まで）。参考表示の title 用 */
  detail?: Record<string, { example_value?: string | null; candidates?: string[] }>;
  pre_activation?: string[];
}

export interface DocumentMeta {
  document_id: string;
  status: string;
  /** 原本ファイル名。一覧・単体の両方で返る（無い行は null）。画面の表示名に使う */
  original_name?: string | null;
  doc_type?: string | null;
  external_ref?: string | null;
  page_count?: number | null;
  /** 単体取得 GET /documents/{id} のみ充填。一覧では空（N+1 回避） */
  pages?: PageDim[];
}

export interface DocumentList {
  items: DocumentMeta[];
  next_cursor?: string | null;
}

export interface ReviewQueueItem {
  document_id: string;
  run_id: string;
  pending: number;
  priority: number;
  /** 原本ファイル名（帳票の original_name）。行の表示名に使う。無い帳票は null */
  original_name?: string | null;
}

export interface CorrectionItem {
  field_name: string;
  original_value?: string | null;
  corrected_value: string;
  note?: string | null;
}

export interface SignedUrl {
  url: string;
  expires_in: number;
}

export interface LockStatus {
  document_id: string;
  locked: boolean;
  held_by_me: boolean;
  holder?: string | null;
  remaining_sec: number;
  ttl_sec: number;
}

export interface ApiError {
  error: { code: string; message: string; details: Record<string, unknown>; request_id: string };
}

// ---- 管理画面（SCR-04/05/06） ----

export interface SchemaFieldDto {
  name: string;
  label?: string | null;
  type: string;
  required: boolean;
  critical: boolean;
  columns?: Record<string, unknown>[] | null;
  /** 読取領域（hint）。null/未設定なら領域指定なし */
  region?: RegionRect | null;
}

export interface SchemaDto {
  id: string;
  doc_type: string;
  version: number;
  fields: SchemaFieldDto[];
  /** 除外領域。doc_type（スキーマ版）単位で決定論的に適用される */
  exclude_regions?: RegionRect[];
  /** テンプレート化時点の帳票ページ数 */
  source_page_count?: number | null;
  /** アーカイブ済み（C9-D）。listSchemas は既定で出さないので、true は includeArchived のときだけ */
  archived?: boolean;
}

/** 取込時の種別指定・分類の候補（GET /doc-types）。fields は含まない軽い一覧。 */
export interface DocTypeDto {
  doc_type: string;
  schema_id: string;
  version: number;
}

export interface ExtractAccepted {
  job_id: string;
  run_id: string;
}

/** POST /documents/extract-batch の 1 件（設計 bulk-processing §2）。 */
export interface ExtractBatchAccepted {
  document_id: string;
  job_id: string;
  run_id: string;
}

/**
 * 投入しなかった帳票と理由。code は単体 /extract の ApiError コード
 * （E1001 不在・E1005 競合/確定済み/確定処理中/他者ロック）か、一括固有の "no_schema"。
 * E1005 は reason（confirmed / in_review / locked / processing / active_run）で
 * 種類が分かる。要約の数え方はこれに依存し、文言には依存させない。
 */
export interface ExtractBatchSkipped {
  document_id: string;
  code: string;
  message: string;
  reason?: string | null;
}

export interface ExtractBatchResponse {
  accepted: ExtractBatchAccepted[];
  skipped: ExtractBatchSkipped[];
  /** doc_type 指定で母集合が上限（200）を超え、新しい順に切り詰めたとき true */
  truncated: boolean;
}

export interface ClassifyCandidate {
  schema_id: string;
  doc_type: string;
  score: number;
}

// 帳票自動分類（⑦）。抽出前にファイル名等から最も近いスキーマを提案する。
export interface ClassifyResult {
  suggested_schema_id?: string | null;
  doc_type?: string | null;
  confidence: number;
  reason: string;
  method: string; // declared / content / filename / heuristic
  candidates: ClassifyCandidate[];
}

export interface JobStatus {
  job_id: string;
  kind: string;
  status: string;
  error_code?: string | null;
}

export interface ConnectionDto {
  id: string;
  type: string;
  name: string;
  config: Record<string, unknown>;
  secret_ref?: string | null;
  allowed_tables: string[];
  status: string;
  created_at?: string | null;
  // 最終同期の結果（⑤⑥ フォルダ監視系。サイレント失敗の可視化）
  last_synced_at?: string | null;
  last_sync_status?: string | null; // ok / error
  last_sync_error?: string | null;
}

// POST /connections/{id}/test の応答。成功で status='tested'。
// postgres は失敗も 200 + ok=false + message、webhook/s3 の失敗は 422（ApiError）
export interface ConnectionTestResult {
  ok: boolean;
  status: string;
  message?: string | null;
}

/** DELETE /connections/{id} の受領書（C9-D） */
export interface ConnectionDeleted {
  connection_id: string;
  deleted: boolean;
  cursors_deleted: number;
  /** gateway が作った秘密（Webhook の署名鍵）を保管先からも消せたか。対象外は null */
  secret_deleted?: boolean | null;
}

/** 409(E1005) の details.workflows に載る「参照しているワークフロー」 */
export interface WorkflowRefDto {
  id: string;
  name: string;
  status: string;
  version: number;
}

export interface RuleDto {
  id: string;
  doc_type?: string | null;
  supplier_key?: string | null;
  field_name?: string | null;
  rule_type: string;
  rule_json: Record<string, unknown>;
  status: string;
  validation_report?: { reproduction_rate?: number; regressions?: number } | null;
  source_correction_ids: string[];
  created_by: string;
  activatable: boolean;
}

export interface ChatConfirmResult {
  ok: boolean;
  message: string;
  detail: Record<string, unknown>;
}

export interface DocumentCreated {
  document_id: string;
  page_count?: number | null;
  status: string;
}

/** DELETE /documents/{id} の受領書。件数は「何を失ったか」をトーストで伝えるために使う。 */
export interface DocumentDeleted {
  document_id: string;
  deleted: boolean;
  objects_deleted: number;
  corrections_deleted: number;
  runs_deleted: number;
}

export interface MetricsResponse {
  total_documents: number;
  status_counts: Record<string, number>;
  stp_rate: number;
  corrections_total: number;
  active_rules: number;
  pending_rules: number;
  memories_total: number;
  field_accuracy_sampled?: number | null;
  llm_cost_jpy_total?: number | null;
}

// ---------- §16 ワークフロー（SCR-07 / P7） ----------

export interface WorkflowNodeDto {
  id: string;
  type: string;
  config: Record<string, unknown>;
  pos?: [number, number] | null;
}

export interface WorkflowGraphDto {
  version: number;
  nodes: WorkflowNodeDto[];
  edges: { from: string; to: string }[];
}

export interface WorkflowDto {
  id: string;
  name: string;
  status: "draft" | "active" | "paused";
  version: number;
  auto_confirm: boolean;
  updated_at?: string | null;
  graph_json: WorkflowGraphDto;
}

/** extract ノードが固定保持している旧版スキーマ（GET /workflows の stale_schema_refs）。
 *  判定は stale-workflows / lint L012 と同じくサーバが行う（schema_id 指定で当該 doc_type
 *  の最新版でない。doc_type 指定のノードは載らない）。設計 §4.4b / §11-9 */
export interface StaleSchemaRefDto {
  node_id: string;
  doc_type: string;
  schema_id: string;
  schema_version: number;
  latest_schema_id: string;
  latest_version: number;
}

export interface WorkflowListItemDto {
  id: string;
  name: string;
  status: string;
  version: number;
  updated_at?: string | null;
  /** 一覧の「旧版スキーマ」バッジの内訳。旧い gateway は返さないので任意 */
  stale_schema_refs?: StaleSchemaRefDto[];
}

/** 当該 doc_type の旧版を extract ノードに固定保持している有効ワークフロー
 *  （GET /schemas/{doc_type}/stale-workflows。設計 §4.4b / D17） */
export interface StaleWorkflowDto {
  id: string;
  name: string;
  status: string;
  version: number;
  schema_id: string;
  schema_version?: number | null;
}

export interface StaleWorkflowList {
  doc_type: string;
  latest_schema_id?: string | null;
  latest_version?: number | null;
  items: StaleWorkflowDto[];
}

export interface CatalogDto {
  types: Record<string, Record<string, unknown>>; // node type -> JSON Schema
  implemented: string[];
}

export interface LintFindingDto {
  rule: string;
  severity: "error" | "warning";
  message: string;
  node_id?: string | null;
}

export interface LintResultDto {
  findings: LintFindingDto[];
  activatable: boolean;
  unsupported_types: string[];
}

export interface SinkPreviewDto {
  node_id: string;
  node_type: string;
  ok: boolean;
  connection_id: string;
  sql?: string | null;
  payload?: Record<string, unknown> | null;
  columns: string[];
  error?: string | null;
}

export interface DryRunResultDto {
  ok: boolean;
  sinks: SinkPreviewDto[];
}

/** runner が射影に残すエラー。message 以外のキーを持つ場合もあるので広めに受ける */
export type WorkflowRunError = { message?: string | null } & Record<string, unknown>;

/**
 * GET /workflows/{id}/runs の 1 件（WorkflowRunSummaryDto）。
 * status: running / waiting_hitl / succeeded / failed / skipped（§16 設計 v0.2 §3）
 */
export interface WorkflowRunItemDto {
  id: string;
  workflow_id: string;
  workflow_version: number;
  document_id?: string | null;
  status: string;
  error?: WorkflowRunError | null;
  started_at?: string | null;
  finished_at?: string | null;
  /** 発火の出所（manual / schedule / s3_event / gdrive_event …）。旧 run や旧 gateway では無い */
  trigger_type?: string | null;
  /** 発火したトリガーノードの id（複数トリガーの WF で経路を示す） */
  trigger_node_id?: string | null;
  /**
   * 帳票の削除で切り離された run（document_id は NULL、未終端なら failed に終端化済み）。
   * document_id が無いだけでは判別できない（schedule 発火の run は最初から帳票を持たない）。
   * 旧 gateway では無い
   */
  document_deleted?: boolean | null;
}

/** workflow_node_runs の 1 行。status: pending / running / succeeded / failed */
export interface WorkflowNodeRunDto {
  node_id: string;
  node_type: string;
  status: string;
  attempt: number;
  output?: Record<string, unknown> | null;
  error?: WorkflowRunError | null;
  started_at?: string | null;
  finished_at?: string | null;
}

/** GET /workflow-runs/{id}。waiting は何を待っているか（await_extract / await_hitl） */
export interface WorkflowRunDto extends WorkflowRunItemDto {
  waiting?: ({ kind?: string; node_id?: string; run_id?: string } & Record<string, unknown>) | null;
  node_runs: WorkflowNodeRunDto[];
}
