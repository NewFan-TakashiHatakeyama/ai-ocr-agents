// gateway-api クライアント（§6）。認証トークンは dev では env、本番はログインフローで差し替える。

import type {
  CorrectionItem,
  DocumentList,
  ResultResponse,
  ReviewQueueItem,
  SignedUrl,
} from "./types";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000/v1";

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
    try {
      const body = await res.json();
      code = body?.error?.code ?? code;
    } catch {
      /* ignore */
    }
    throw new Error(`API error ${code} (${res.status})`);
  }
  return (await res.json()) as T;
}

export const api = {
  listDocuments: (status?: string) =>
    request<DocumentList>(`/documents${status ? `?status=${status}` : ""}`),

  getResult: (documentId: string) =>
    request<ResultResponse>(`/documents/${documentId}/result`),

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
};
