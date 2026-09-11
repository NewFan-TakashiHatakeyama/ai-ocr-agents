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
  mismatch_fields?: string[];
  layout_mismatch?: boolean;
  /** 読取領域ヒント（設計 region-field-add-and-hint-v2 §2.5・§2.6）。ヒント有効時のみ */
  hints?: RegionHintStats;
}

/** ヒントの内訳。given は渡した項目、dropped は渡す前に落とした項目と理由、
 *  outcomes は LLM が従ったか（span_ids と候補の集合演算。モデルの申告ではない）。
 *  pre_activation は評価した項目のうち、領域がヒント有効化（2026-09-12）より前に
 *  引かれたもの（created_at が無い・古い。§1.5）。参考表示にしか使わない。 */
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

export interface WorkflowListItemDto {
  id: string;
  name: string;
  status: string;
  version: number;
  updated_at?: string | null;
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

export interface WorkflowRunItemDto {
  id: string;
  workflow_id: string;
  workflow_version: number;
  document_id?: string | null;
  status: string;
  started_at?: string | null;
  finished_at?: string | null;
}
