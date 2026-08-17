// gateway-api クライアント（§6）。認証トークンは dev では env、本番はログインフローで差し替える。

import type {
  CatalogDto,
  ConnectionDto,
  DryRunResultDto,
  ExtractAccepted,
  JobStatus,
  LintResultDto,
  WorkflowDto,
  WorkflowGraphDto,
  WorkflowListItemDto,
  WorkflowRunItemDto,
  ChatConfirmResult,
  CorrectionItem,
  DocumentCreated,
  DocumentList,
  LockStatus,
  MetricsResponse,
  ResultResponse,
  ReviewQueueItem,
  RuleDto,
  SchemaDto,
  SchemaFieldDto,
  SignedUrl,
} from "./types";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000/v1";

export class ApiError extends Error {
  status: number;
  code: string;
  /** gateway の error.details（E4001 の検証エラー一覧など）。UI で位置を示すのに使う */
  details?: Record<string, unknown>;
  constructor(
    status: number,
    code: string,
    message?: string,
    details?: Record<string, unknown>,
  ) {
    super(message ?? `API error ${code} (${status})`);
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

function token(): string | undefined {
  if (typeof window !== "undefined") {
    const stored = window.localStorage.getItem("nf_token");
    if (stored) return stored;
  }
  return process.env.NEXT_PUBLIC_DEV_TOKEN || undefined;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  const t = token();
  if (t) headers.set("Authorization", `Bearer ${t}`);
  if (init?.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const res = await fetch(`${API_BASE}${path}`, { ...init, headers });
  if (!res.ok) {
    let code = String(res.status);
    let message: string | undefined;
    let details: Record<string, unknown> | undefined;
    try {
      const body = await res.json();
      code = body?.error?.code ?? code;
      message = body?.error?.message;
      details = body?.error?.details;
    } catch {
      /* ignore */
    }
    throw new ApiError(res.status, code, message, details);
  }
  return (await res.json()) as T;
}

export const api = {
  listDocuments: (status?: string) =>
    request<DocumentList>(`/documents${status ? `?status=${status}` : ""}`),

  getResult: (documentId: string) =>
    request<ResultResponse>(`/documents/${documentId}/result`),

  // 抽出（AI-OCR）を開始する。schema_id 未指定でも走るが、項目を出すにはスキーマ指定を推奨。
  // Idempotency-Key で連打・再送の二重 run を防ぐ（gateway が同キーをキャッシュ応答する）。
  extract: (
    documentId: string,
    opts?: { schema_id?: string; force_vl?: boolean; idempotencyKey?: string },
  ) =>
    request<ExtractAccepted>(`/documents/${documentId}/extract`, {
      method: "POST",
      headers: opts?.idempotencyKey ? { "Idempotency-Key": opts.idempotencyKey } : undefined,
      body: JSON.stringify({
        schema_id: opts?.schema_id ?? null,
        options: { force_vl: opts?.force_vl ?? false },
      }),
    }),

  getJob: (jobId: string) => request<JobStatus>(`/jobs/${jobId}`),

  pageImage: (documentId: string, pageNo: number) =>
    request<SignedUrl>(`/documents/${documentId}/pages/${pageNo}/image`),

  reviewQueue: () => request<{ items: ReviewQueueItem[] }>(`/review/queue`),

  postCorrections: (documentId: string, runId: string, version: number, items: CorrectionItem[]) =>
    request<{ correction_ids: string[] }>(`/documents/${documentId}/corrections`, {
      method: "POST",
      body: JSON.stringify({ run_id: runId, version, items }),
    }),

  confirm: (documentId: string, runId: string) =>
    request<{ status: string }>(`/documents/${documentId}/confirm`, {
      method: "POST",
      body: JSON.stringify({ run_id: runId }),
    }),

  // 検証画面ソフトロック（§8.2）: マウントで acquire、定期 heartbeat、離脱で release。
  acquireLock: (documentId: string) =>
    request<LockStatus>(`/documents/${documentId}/lock`, { method: "POST" }),
  releaseLock: (documentId: string) =>
    request<LockStatus>(`/documents/${documentId}/lock`, { method: "DELETE" }),

  // 管理画面（SCR-04/05/06, admin）
  listSchemas: () => request<{ items: SchemaDto[] }>(`/schemas`),
  putSchema: (docType: string, fields: SchemaFieldDto[]) =>
    request<SchemaDto>(`/schemas`, {
      method: "PUT",
      body: JSON.stringify({ doc_type: docType, fields }),
    }),
  listConnections: () => request<{ items: ConnectionDto[] }>(`/connections`),
  listRules: (status?: string) =>
    request<{ items: RuleDto[] }>(`/rules${status ? `?status=${status}` : ""}`),
  patchRule: (ruleId: string, status: string) =>
    request<RuleDto>(`/rules/${ruleId}`, {
      method: "PATCH",
      body: JSON.stringify({ status }),
    }),
  metrics: () => request<MetricsResponse>(`/metrics/summary`),

  // チャットホーム（SCR-01）。SSE を fetch ストリームで購読する。
  chatStream: async (message: string, onEvent: (type: string, data: Record<string, unknown>) => void) => {
    const headers = new Headers({ "Content-Type": "application/json" });
    const t = token();
    if (t) headers.set("Authorization", `Bearer ${t}`);
    const res = await fetch(`${API_BASE}/chat`, { method: "POST", headers, body: JSON.stringify({ message }) });
    if (!res.ok || !res.body) throw new ApiError(res.status, String(res.status));
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const blocks = buf.split("\n\n");
      buf = blocks.pop() ?? "";
      for (const block of blocks) {
        let ev = "message";
        let data = "{}";
        for (const line of block.split("\n")) {
          if (line.startsWith("event: ")) ev = line.slice(7);
          else if (line.startsWith("data: ")) data = line.slice(6);
        }
        onEvent(ev, JSON.parse(data));
      }
    }
  },

  chatConfirm: (action: string, params: Record<string, unknown>) =>
    request<ChatConfirmResult>(`/chat/confirm`, {
      method: "POST",
      body: JSON.stringify({ action, params }),
    }),

  // ---------- §16 ワークフロー（SCR-07 / P7） ----------
  workflowCatalog: () => request<CatalogDto>(`/workflows/catalog`),
  listWorkflows: () => request<{ items: WorkflowListItemDto[] }>(`/workflows`),
  getWorkflow: (id: string) => request<WorkflowDto>(`/workflows/${id}`),
  createWorkflow: (name: string, graph: WorkflowGraphDto) =>
    request<WorkflowDto>(`/workflows`, {
      method: "POST",
      body: JSON.stringify({ name, graph_json: graph }),
    }),
  putWorkflow: (id: string, name: string, graph: WorkflowGraphDto) =>
    request<WorkflowDto>(`/workflows/${id}`, {
      method: "PUT",
      body: JSON.stringify({ name, graph_json: graph }),
    }),
  lintWorkflow: (id: string, graph?: WorkflowGraphDto) =>
    request<LintResultDto>(`/workflows/${id}/lint`, {
      method: "POST",
      body: graph ? JSON.stringify({ graph_json: graph }) : undefined,
    }),
  dryRunWorkflow: (id: string) =>
    request<DryRunResultDto>(`/workflows/${id}/dry-run`, { method: "POST" }),
  activateWorkflow: (id: string) =>
    request<WorkflowDto>(`/workflows/${id}/activate`, { method: "POST" }),
  pauseWorkflow: (id: string) =>
    request<WorkflowDto>(`/workflows/${id}/pause`, { method: "POST" }),
  listWorkflowRuns: (id: string) =>
    request<{ items: WorkflowRunItemDto[] }>(`/workflows/${id}/runs`),

  uploadDocument: (file: File, docType?: string) => {
    const fd = new FormData();
    fd.append("file", file);
    if (docType) fd.append("doc_type", docType);
    const headers = new Headers();
    const t = token();
    if (t) headers.set("Authorization", `Bearer ${t}`);
    return fetch(`${API_BASE}/documents`, { method: "POST", headers, body: fd }).then(async (res) => {
      if (!res.ok) {
        let code = String(res.status);
        try {
          code = (await res.json())?.error?.code ?? code;
        } catch {
          /* ignore */
        }
        throw new ApiError(res.status, code);
      }
      return (await res.json()) as DocumentCreated;
    });
  },
};
