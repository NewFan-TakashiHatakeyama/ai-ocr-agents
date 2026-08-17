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
  result_version: number;
  engine_versions: Record<string, unknown>;
  fields: ExtractedField[];
  tables: TableResult[];
  review_summary: Record<string, number>;
  fallback_pages?: number[]; // VL フォールバックしたページ（§5.4）
}

export interface DocumentMeta {
  document_id: string;
  status: string;
  doc_type?: string | null;
  external_ref?: string | null;
  page_count?: number | null;
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
}

export interface SchemaDto {
  id: string;
  doc_type: string;
  version: number;
  fields: SchemaFieldDto[];
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
