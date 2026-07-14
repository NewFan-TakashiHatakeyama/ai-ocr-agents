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
  doc_type: string;
  version: number;
  fields: SchemaFieldDto[];
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
