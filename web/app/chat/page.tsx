"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useRef, useState } from "react";

import { AppShell } from "@/components/AppShell";
import { ApiError, api } from "@/lib/api";
import { CHAT_DOC_TYPE_PARAM, schemaAddRequest } from "@/lib/schemaChat";
import { useToasts } from "@/lib/toast";
import { UPLOAD_ACCEPT, UPLOAD_FORMATS_HINT, UPLOAD_FORMATS_LABEL } from "@/lib/uploads";

// SCR-01 チャットホーム（§3.3/§4.5）。生成AIの入口。書込み系は承認カードを挟む。
interface ToolCall {
  name: string;
  target?: string;
  label?: string;
  steps?: string[];
}
interface Confirm {
  action: string;
  prompt: string;
  doc_type?: string;
  field?: Record<string, unknown>;
}
interface Msg {
  id: number;
  role: "user" | "ai";
  text: string;
  tools: ToolCall[];
  confirm?: Confirm;
  streaming?: boolean;
}

const SUGGESTIONS = ["要確認の請求書を見せて", "スキーマに「支払方法」を追加して", "先月のSTP率は？"];

function ChatInner() {
  // スキーマ管理の「チャットで追加を依頼」から来たときは ?doc_type=<開いていたスキーマ>。
  // 入力欄をその doc_type 入りの依頼文で始める（送信はしない。項目名は書き換える前提）。
  // 素の依頼だとエージェントは対象を invoice に倒すので、ここで対象を文に入れておく。
  const params = useSearchParams();
  const fromSchema = params.get(CHAT_DOC_TYPE_PARAM);
  const [messages, setMessages] = useState<Msg[]>([]);
  const [input, setInput] = useState(() => (fromSchema ? schemaAddRequest(fromSchema) : ""));
  const [busy, setBusy] = useState(false);
  const push = useToasts((s) => s.push);
  const router = useRouter();
  const fileRef = useRef<HTMLInputElement>(null);
  const idRef = useRef(0);

  function patch(id: number, fn: (m: Msg) => Msg) {
    setMessages((ms) => ms.map((m) => (m.id === id ? fn(m) : m)));
  }

  async function send(text: string) {
    const t = text.trim();
    if (!t || busy) return;
    setInput("");
    const userId = ++idRef.current;
    const aiId = ++idRef.current;
    setMessages((m) => [
      ...m,
      { id: userId, role: "user", text: t, tools: [] },
      { id: aiId, role: "ai", text: "", tools: [], streaming: true },
    ]);
    setBusy(true);
    try {
      await api.chatStream(t, (type, data) => {
        if (type === "token") patch(aiId, (m) => ({ ...m, text: m.text + ((data.text as string) ?? "") }));
        else if (type === "tool_call") patch(aiId, (m) => ({ ...m, tools: [...m.tools, data as unknown as ToolCall] }));
        else if (type === "confirm_request") patch(aiId, (m) => ({ ...m, confirm: data as unknown as Confirm }));
      });
    } catch (e) {
      push({ kind: "err", message: `応答に失敗しました（${(e as Error).message}）。` });
    } finally {
      patch(aiId, (m) => ({ ...m, streaming: false }));
      setBusy(false);
    }
  }

  async function approve(id: number, c: Confirm) {
    try {
      const r = await api.chatConfirm(c.action, { doc_type: c.doc_type, field: c.field });
      push({ kind: r.ok ? "ok" : "warn", message: r.message });
      patch(id, (m) => ({ ...m, confirm: undefined }));
      if (r.ok) router.push("/schemas");
    } catch (e) {
      push({
        kind: "warn",
        message:
          e instanceof ApiError && e.status === 403
            ? "この操作には管理者権限が必要です。"
            : "実行に失敗しました。時間をおいて再試行してください。",
      });
    }
  }

  async function onUpload(file: File) {
    push({ kind: "info", message: `${file.name} をアップロード中…` });
    try {
      const doc = await api.uploadDocument(file);
      const aiId = ++idRef.current;
      setMessages((m) => [
        ...m,
        {
          id: aiId,
          role: "ai",
          text: `「${file.name}」を取り込みました。スキーマ（テンプレート）が未登録でも、まず項目を自動発見して値を抽出できます。結果を見てからテンプレート化してください。`,
          tools: [{ name: "navigate", target: `/documents/${doc.document_id}`, label: "ドキュメントを開く" }],
        },
      ]);
    } catch (e) {
      // 対応形式は lib/uploads から出す。以前ここに「Word/Excel に対応」と書いていたが、
      // ingest は Office を E1003（変換未実装）で必ず拒否するので嘘だった。
      push({
        kind: "warn",
        message: `アップロードできませんでした（${e instanceof ApiError ? e.code : ""}）。${UPLOAD_FORMATS_HINT}`,
      });
    }
  }

  const empty = messages.length === 0;

  return (
    <AppShell active="chat">
      <div className="chat-shell">
        <div className={`chat-scroll${empty ? " empty" : ""}`} aria-live="polite">
          {empty && (
            <div className="hello">
              こんにちは。<b>帳票を任せてください。</b>
            </div>
          )}
          {messages.map((msg) => (
            <div key={msg.id} className="msg">
              {msg.role === "user" ? (
                <div className="bubble-user">{msg.text}</div>
              ) : (
                <>
                  <div className="bubble-ai">
                    {msg.text}
                    {msg.streaming && <span className="spin" style={{ display: "inline-block", width: 12, height: 12, marginLeft: 6, verticalAlign: "middle" }} />}
                  </div>
                  {msg.tools.map((tool, i) =>
                    tool.name === "navigate" ? (
                      <div key={i} className="tool-card">
                        <span>🔗</span>
                        <span style={{ flex: 1 }}>{tool.label}</span>
                        <Link className="btn sm primary" href={tool.target ?? "#"}>
                          開く
                        </Link>
                      </div>
                    ) : tool.name === "progress" ? (
                      <div key={i} className="tool-card">
                        <span className="spin" />
                        <div style={{ flex: 1 }}>
                          <b>抽出処理</b>
                          <div className="proc-steps" style={{ marginTop: 6 }}>
                            {tool.steps?.map((s) => (
                              <span key={s} className="step">
                                {s}
                              </span>
                            ))}
                          </div>
                        </div>
                        <span className="chip st-processing">処理中</span>
                      </div>
                    ) : null,
                  )}
                  {msg.confirm && (
                    <div className="confirm-card">
                      <div className="cc-head">
                        <b>エージェントの提案：</b>
                        <span style={{ color: "var(--ink2)", flex: 1 }}>{msg.confirm.prompt}</span>
                        {/* どのスキーマの新版になるかを承認前に見せる。prompt はエージェントが
                            書く文で対象を含むとは限らず、承認後にサーバは doc_type 無しを
                            invoice として扱う。対象が読めない提案は承認させない */}
                        {msg.confirm.action === "update_schema" && (
                          <span className="sub" title="承認するとこのスキーマの新しい版が作られます">
                            対象スキーマ: <b>{msg.confirm.doc_type ?? "（不明）"}</b>
                          </span>
                        )}
                        <button
                          className="btn sm primary"
                          disabled={msg.confirm.action === "update_schema" && !msg.confirm.doc_type}
                          title={
                            msg.confirm.action === "update_schema" && !msg.confirm.doc_type
                              ? "対象のスキーマが特定できません。「<スキーマ名> のスキーマに…」のように言い直してください"
                              : undefined
                          }
                          onClick={() => approve(msg.id, msg.confirm!)}
                        >
                          承認して実行
                        </button>
                        <button className="btn sm ghost" onClick={() => patch(msg.id, (m) => ({ ...m, confirm: undefined }))}>
                          今回はしない
                        </button>
                      </div>
                    </div>
                  )}
                </>
              )}
            </div>
          ))}
        </div>

        <div className="chat-composer">
          {empty && (
            <div className="suggs">
              {SUGGESTIONS.map((s) => (
                <button key={s} className="sugg" onClick={() => send(s)}>
                  {s}
                </button>
              ))}
            </div>
          )}
          <div className="chat-card">
            <div className="chat-input">
              <input
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && send(input)}
                placeholder="帳票をアップロード、または質問・指示を入力…"
                aria-label="メッセージ入力"
              />
            </div>
            <div className="chat-tools">
              <input
                ref={fileRef}
                type="file"
                accept={UPLOAD_ACCEPT}
                style={{ display: "none" }}
                onChange={(e) => {
                  const f = e.target.files?.[0];
                  if (f) onUpload(f);
                  e.target.value = "";
                }}
              />
              <span className="tool-pill" onClick={() => fileRef.current?.click()}>
                📎 ファイル（{UPLOAD_FORMATS_LABEL}）
              </span>
              <span className="tool-pill">請求書スキーマ v4</span>
              <button className="send" disabled={busy || !input.trim()} onClick={() => send(input)} aria-label="送信">
                <svg viewBox="0 0 24 24">
                  <path d="M12 19V6M6 12l6-6 6 6" />
                </svg>
              </button>
            </div>
          </div>
        </div>
      </div>
    </AppShell>
  );
}

// useSearchParams は Suspense 境界内で使う（Next 15）。
export default function ChatPage() {
  return (
    <Suspense fallback={<div className="page">読み込み中…</div>}>
      <ChatInner />
    </Suspense>
  );
}
