// gateway-api クライアント（§6）。認証トークンは dev では env、本番はログインフローで差し替える。

import type {
  StaleWorkflowList,
  CatalogDto,
  ClassifyResult,
  ConnectionDeleted,
  ConnectionDto,
  ConnectionTestResult,
  DryRunResultDto,
  ExtractAccepted,
  ExtractBatchResponse,
  JobStatus,
  LintResultDto,
  WorkflowDto,
  WorkflowGraphDto,
  WorkflowListItemDto,
  WorkflowRunDto,
  WorkflowRunItemDto,
  ChatConfirmResult,
  CorrectionItem,
  DocumentCreated,
  DocumentDeleted,
  DocumentList,
  DocumentMeta,
  DocTypeDto,
  ExampleValueCheckResponse,
  LockStatus,
  MetricsResponse,
  PutSchemaResponse,
  ResultResponse,
  RegionRect,
  ReviewQueueItem,
  RuleDto,
  RunSpans,
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

  // 最新 run の OCR span（除外領域の適用後）をページ単位で引く（設計 D12）。
  // テンプレート化画面で「枠に含まれる文字」を出し、例示値の出どころにする。
  // 未抽出（run が無い）は 400/E1001。行が無いページは spans が空で返る。
  getRunSpans: (documentId: string, page: number) =>
    request<RunSpans>(`/documents/${documentId}/spans?page=${page}`),

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

  // 複数帳票の抽出をまとめて投入する（設計 bulk-processing §2）。対象は
  // document_ids か doc_type のどちらか一方。帳票ごとの拒否（確定済み・処理中・
  // スキーマなし・不在）は skipped に理由付きで返り、HTTP は 202 のまま。
  // 1 件の事情で全体が 4xx にはならないので、呼び出し側は必ず skipped を読んで伝える。
  extractBatch: (
    target: { document_ids: string[] } | { doc_type: string; statuses?: string[] },
    opts?: { schema_id?: string; supersede_review?: boolean; idempotencyKey?: string },
  ) =>
    request<ExtractBatchResponse>(`/documents/extract-batch`, {
      method: "POST",
      headers: opts?.idempotencyKey ? { "Idempotency-Key": opts.idempotencyKey } : undefined,
      body: JSON.stringify({
        ...target,
        schema_id: opts?.schema_id ?? null,
        supersede_review: opts?.supersede_review ?? false,
        options: {},
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

  // 取込時の種別指定・分類の候補。**listSchemas は admin 限定**なので、
  // uploader/reviewer でも引けるこちらを使う（fields は返らない）。
  listDocTypes: () => request<{ items: DocTypeDto[] }>(`/doc-types`),
  // 帳票メタの部分更新（v1 は doc_type のみ、admin）。テンプレート化の書き戻し用。
  patchDocument: (documentId: string, body: { doc_type: string }) =>
    request<DocumentMeta>(`/documents/${documentId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  // 管理画面（SCR-04/05/06, admin）。アーカイブ済み（C9-D）は既定で出さない——
  // 抽出のスキーマ選択・ワークフローの extract ノードはこの一覧を候補にするので、
  // 隠すだけで「新しく使われる」経路が塞がる。管理画面の表示切替だけが true を渡す
  listSchemas: (opts?: { includeArchived?: boolean }) =>
    request<{ items: SchemaDto[] }>(
      `/schemas${opts?.includeArchived ? "?include_archived=true" : ""}`,
    ),
  // アーカイブ / 復元。全版の is_active を切り替えるだけで行は消えない（過去の抽出結果
  // から定義を辿れる）。有効なワークフローが使っていれば 409(E1005, details.workflows)
  archiveSchema: (docType: string) =>
    request<SchemaDto>(`/schemas/${encodeURIComponent(docType)}/archive`, { method: "POST" }),
  unarchiveSchema: (docType: string) =>
    request<SchemaDto>(`/schemas/${encodeURIComponent(docType)}/unarchive`, { method: "POST" }),
  // doc_type の**最新版**を取る（領域編集のプリロード起点）。listSchemas でも
  // 最新版は取れるが、run.schema_id は抽出時点の旧版であり得るので id 突合は
  // できない。編集は必ず doc_type 起点で行う。
  // 当該 doc_type の旧版を固定保持している有効ワークフロー（設計 §4.4b）。判定は
  // サーバが**全旧版**で行う（web で直前の版だけ突合すると v1 固定が漏れる）
  staleWorkflows: (docType: string) =>
    request<StaleWorkflowList>(`/schemas/${encodeURIComponent(docType)}/stale-workflows`),
  getSchema: (docType: string) =>
    request<SchemaDto>(`/schemas/${encodeURIComponent(docType)}`),
  // create=true は新規作成モード: 既存 doc_type ならサーバが E1005(409) で拒否する
  //（クライアントの重複チェックは一覧が陳腐化していると素通りするため）
  // exclude_regions / source_page_count は **キー自体を送らなければ直前版から引き継ぎ**
  // される（サーバ側 §4.4）。undefined を明示的に送ると JSON.stringify が落とすので
  // 結果は同じだが、「省略＝引き継ぎ」を呼び出し側が意識できるよう opts で分ける。
  // 応答は保存した版に warnings（記入例語の例示値など）を足したもの。保存は止めない
  // ので、読むかどうかは呼び出し側が決める（テンプレート化画面は保存後の通知に出す）
  putSchema: (
    docType: string,
    fields: SchemaFieldDto[],
    opts?: {
      create?: boolean;
      excludeRegions?: RegionRect[] | null;
      sourcePageCount?: number | null;
    },
  ) =>
    request<PutSchemaResponse>(`/schemas`, {
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
  // 例示値が記入例語（「〇〇株式会社」「YYYY/MM/DD」）かをサーバに判定させる（admin）。
  // 規則は orchestrator の事前ガードと同じ関数にあり、web では書き直さない（lib/placeholderExample）
  checkExampleValues: (values: (string | null)[]) =>
    request<ExampleValueCheckResponse>(`/schemas/example-values/check`, {
      method: "POST",
      body: JSON.stringify({ values }),
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
  // 疎通テスト（postgres: SELECT 1 / webhook: 署名付き test イベント / s3: HeadBucket）。
  // 成功で status='tested'（ワークフローの有効化に使える）。postgres の失敗は
  // 200 + ok=false、webhook/s3 の失敗は 422（ApiError）で理由が返る
  testConnection: (connectionId: string) =>
    request<ConnectionTestResult>(`/connections/${connectionId}/test`, { method: "POST" }),
  // 接続の無効化 / 再有効化（C9-D）。有効なワークフローが使っている接続の無効化は
  // 409(E1005, details.workflows) で断られる（先にワークフローを停止する）。
  // 再有効化の着地点は型で違う: postgres は untested に戻る（疎通テストを通すまで
  // ワークフローに使えない）。webhook / s3 / フォルダ監視系は active。応答の status を見る
  patchConnectionStatus: (connectionId: string, status: "active" | "disabled") =>
    request<ConnectionDto>(`/connections/${connectionId}`, {
      method: "PATCH",
      body: JSON.stringify({ status }),
    }),
  // 接続の削除。どのワークフロー版（定義・実行のスナップショット）からも参照されて
  // いない接続だけ消せる。参照があれば 409(E1005, details.reason="referenced")。
  // Webhook は gateway が作った署名鍵も保管先から消す（secret_deleted）
  deleteConnection: (connectionId: string) =>
    request<ConnectionDeleted>(`/connections/${connectionId}`, { method: "DELETE" }),
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

  // 承認カードの実行。Idempotency-Key は extract と同じ扱い（gateway が同キーを
  // キャッシュ応答する）。カードごとに 1 つ鍵を作って再送でも使い回すと、連打や
  // ネットワーク断後の再試行で rerun_extract が二重に Run を発行しない。
  chatConfirm: (action: string, params: Record<string, unknown>, opts?: { idempotencyKey?: string }) =>
    request<ChatConfirmResult>(`/chat/confirm`, {
      method: "POST",
      headers: opts?.idempotencyKey ? { "Idempotency-Key": opts.idempotencyKey } : undefined,
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
  // 削除（C9-D）。draft/paused で実行履歴が無いものだけ。active は 409(reason=active)、
  // 履歴があれば 409(reason=has_runs) — その場合は停止のまま残す
  deleteWorkflow: (id: string) =>
    request<{ workflow_id: string; deleted: boolean }>(`/workflows/${id}`, {
      method: "DELETE",
    }),
  // 実行履歴（新しい順、既定 50 件）。running / waiting_hitl があるあいだ UI が 5 秒ごとに引く
  listWorkflowRuns: (id: string) =>
    request<{ items: WorkflowRunItemDto[] }>(`/workflows/${id}/runs`),
  // 1 run の詳細（node_runs 付き、viewer 可）
  getWorkflowRun: (runId: string) => request<WorkflowRunDto>(`/workflow-runs/${runId}`),
  // failed の run を失敗セグメントから再実行する（admin）。完了済みノードは走り直さない
  //（§6.5 の再実行境界）。failed 以外は 409(E1005)。呼ぶ前に確認を取ること
  retryWorkflowRun: (runId: string) =>
    request<{ workflow_run_id: string; workflow_version: number }>(
      `/workflow-runs/${runId}/retry`,
      { method: "POST" },
    ),

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
