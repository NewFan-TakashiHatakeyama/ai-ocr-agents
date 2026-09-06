// gateway-api クライアント（§6）。認証トークンは dev では env、本番はログインフローで差し替える。

import type {
  CatalogDto,
  ClassifyResult,
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
  DocumentDeleted,
  DocumentList,
  DocumentMeta,
  LockStatus,
  MetricsResponse,
  ResultResponse,
  RegionRect,
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

  getDocument: (documentId: string) =>
    request<DocumentMeta>(`/documents/${documentId}`),

  getResult: (documentId: string) =>
    request<ResultResponse>(`/documents/${documentId}/result`),

  // 取り込んだ帳票を消す（原本・ページ画像・抽出結果・学習例まで。復元不可）。
  // 呼ぶ前に必ず確認を取ること。409(E1005) は処理中/他者ロック中で、時間をおけば通る。
  deleteDocument: (documentId: string) =>
    request<DocumentDeleted>(`/documents/${documentId}`, { method: "DELETE" }),

  // 抽出（AI-OCR）を開始する。schema_id 未指定は自動発見モード（ADR-0006 の既定導線。
  // 帳票から見出し＋値の組を LLM が発見する）。指定すればその定義で抽出する。
  // Idempotency-Key で連打・再送の二重 run を防ぐ（gateway が同キーをキャッシュ応答する）。
  extract: (
    documentId: string,
    opts?: {
      schema_id?: string;
      force_vl?: boolean;
      idempotencyKey?: string;
      // レビュー待ちの帳票を取り直す。既定（false）は processing と needs_review の
      // 両方を競合とみなすため、テンプレート化直後の再抽出は必ず 409 になる。
      supersede_review?: boolean;
    },
  ) =>
    request<ExtractAccepted>(`/documents/${documentId}/extract`, {
      method: "POST",
      headers: opts?.idempotencyKey ? { "Idempotency-Key": opts.idempotencyKey } : undefined,
      body: JSON.stringify({
        schema_id: opts?.schema_id ?? null,
        options: { force_vl: opts?.force_vl ?? false },
        supersede_review: opts?.supersede_review ?? false,
      }),
    }),

  getJob: (jobId: string) => request<JobStatus>(`/jobs/${jobId}`),

  // 帳票種別を推定して最も近いスキーマを提案する（⑦, 抽出前サジェスト）
  classifyDocument: (documentId: string) =>
    request<ClassifyResult>(`/documents/${documentId}/classify`, { method: "POST" }),

  // 手元の帳票を active なワークフローに手動投入する（source.manual, §7.1）
  startWorkflowRun: (workflowId: string, documentId: string) =>
    request<{ workflow_run_id: string; workflow_version: number }>(
      `/workflows/${workflowId}/runs`,
      { method: "POST", body: JSON.stringify({ document_id: documentId }) },
    ),

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
  // doc_type の**最新版**を取る（領域編集のプリロード起点）。listSchemas でも
  // 最新版は取れるが、run.schema_id は抽出時点の旧版であり得るので id 突合は
  // できない。編集は必ず doc_type 起点で行う。
  getSchema: (docType: string) =>
    request<SchemaDto>(`/schemas/${encodeURIComponent(docType)}`),
  // create=true は新規作成モード: 既存 doc_type ならサーバが E1005(409) で拒否する
  //（クライアントの重複チェックは一覧が陳腐化していると素通りするため）
  // exclude_regions / source_page_count は **キー自体を送らなければ直前版から引き継ぎ**
  // される（サーバ側 §4.4）。undefined を明示的に送ると JSON.stringify が落とすので
  // 結果は同じだが、「省略＝引き継ぎ」を呼び出し側が意識できるよう opts で分ける。
  putSchema: (
    docType: string,
    fields: SchemaFieldDto[],
    opts?: {
      create?: boolean;
      excludeRegions?: RegionRect[] | null;
      sourcePageCount?: number | null;
    },
  ) =>
    request<SchemaDto>(`/schemas`, {
      method: "PUT",
      body: JSON.stringify({
        doc_type: docType,
        fields,
        create: opts?.create ?? false,
        ...(opts?.excludeRegions !== undefined
          ? { exclude_regions: opts.excludeRegions }
          : {}),
        ...(opts?.sourcePageCount !== undefined
          ? { source_page_count: opts.sourcePageCount }
          : {}),
      }),
    }),
  listConnections: () => request<{ items: ConnectionDto[] }>(`/connections`),
  // 接続の登録（⑤⑥ SaaS連携）。秘密は config に入れず secret_ref で渡す（§16.5）
  createConnection: (input: {
    type: string;
    name: string;
    config: Record<string, unknown>;
    secret_ref?: string | null;
  }) =>
    request<ConnectionDto>(`/connections`, { method: "POST", body: JSON.stringify(input) }),
  // 「今すぐ同期」: gdrive 接続の監視フォルダを即時に差分検知する（worker が実行）
  syncConnection: (connectionId: string) =>
    request<{ queued: boolean }>(`/connections/${connectionId}/sync`, { method: "POST" }),
  listRules: (status?: string) =>
    request<{ items: RuleDto[] }>(`/rules${status ? `?status=${status}` : ""}`),
  patchRule: (ruleId: string, status: string) =>
    request<RuleDto>(`/rules/${ruleId}`, {
      method: "PATCH",
      body: JSON.stringify({ status }),
    }),
  // LLM最適化ヒント（llm_hint）を人が直接オーサリングする（③）
  createLlmHint: (input: {
    doc_type: string;
    field_name?: string | null;
    hint_text: string;
    description?: string | null;
  }) =>
    request<RuleDto>(`/rules`, { method: "POST", body: JSON.stringify(input) }),
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
