"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useRef, useState } from "react";

import { AppShell } from "@/components/AppShell";
import { ApiError, api } from "@/lib/api";
import { type Confirm, confirmFailure, describeConfirm, resultLink, splitConfirm } from "@/lib/chatConfirm";
import { CHAT_DOC_TYPE_PARAM, schemaAddRequest } from "@/lib/schemaChat";
import { useToasts } from "@/lib/toast";
import { UPLOAD_ACCEPT, UPLOAD_FORMATS_HINT, UPLOAD_FORMATS_LABEL } from "@/lib/uploads";
import { newUuid } from "@/lib/uuid";

// SCR-01 チャットホーム（§3.3/§4.5）。生成AIの入口。書込み系は承認カードを挟む。
interface ToolCall {
  name: string;
  target?: string;
  label?: string;
  steps?: string[];
}
// 承認カードの実行結果（POST /chat/confirm の ok / message + 開ける画面）
interface ConfirmResult {
  ok: boolean;
  message: string;
  link: { href: string; label: string } | null;
}
interface Msg {
  id: number;
  role: "user" | "ai";
  text: string;
  tools: ToolCall[];
  confirm?: Confirm;
  // カードごとの Idempotency-Key。confirm_request を受けた時に 1 つ作り、そのカードの
  // 承認（再試行を含む）で使い回す。連打・ネットワーク断後の再送で二重実行しない
  confirmKey?: string;
  result?: ConfirmResult;
  streaming?: boolean;
}

const SUGGESTIONS = ["要確認の請求書を見せて", "スキーマに「支払方法」を追加して", "先月のSTP率は？"];

// update_schema の承認カードが対象にするスキーマ。confirm_request は平坦なオブジェクトで
// 追加のキーは unknown なので、文字列のときだけ名前として扱う（空文字は「不明」）。
function targetDocType(c: Confirm): string | undefined {
  return typeof c.doc_type === "string" && c.doc_type ? c.doc_type : undefined;
}

function ChatInner() {
  // スキーマ管理の「チャットで追加を依頼」から来たときは ?doc_type=<開いていたスキーマ>。
  // 入力欄をその doc_type 入りの依頼文で始める（送信はしない。項目名は書き換える前提）。
  // 素の依頼だとエージェントは対象を invoice に倒すので、ここで対象を文に入れておく。
  const params = useSearchParams();
  const fromSchema = params.get(CHAT_DOC_TYPE_PARAM);
  const [messages, setMessages] = useState<Msg[]>([]);
  const [input, setInput] = useState(() => (fromSchema ? schemaAddRequest(fromSchema) : ""));
  const [busy, setBusy] = useState(false);
  // 承認実行中のカード（メッセージ id）。ボタンの無効化は state、二重送信の判定は ref で
  // 行う（連打の 2 回目は再描画前に届き得るので、state だけでは防げない）
  const [approving, setApproving] = useState<number | null>(null);
  const approvingRef = useRef<number | null>(null);
  const push = useToasts((s) => s.push);
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
        else if (type === "confirm_request")
          patch(aiId, (m) => ({ ...m, confirm: data as unknown as Confirm, confirmKey: newUuid() }));
      });
    } catch (e) {
      push({ kind: "err", message: `応答に失敗しました（${(e as Error).message}）。` });
    } finally {
      patch(aiId, (m) => ({ ...m, streaming: false }));
      setBusy(false);
    }
  }

  async function approve(id: number, c: Confirm, key: string | undefined) {
    // 実行中は受け付けない（連打で 2 通目が送られると、2 通目の ok=false「現在処理中」が
    // 1 通目の成功とリンクを上書きしてしまう）。鍵はサーバ側の保険（同キーはキャッシュ応答）
    if (approvingRef.current !== null) return;
    approvingRef.current = id;
    setApproving(id);
    // confirm_request の残り（action / prompt 以外）をそのまま返す。update_schema だけ
    // でなく rerun_extract（document_id / schema_id）・manage_rules（rule_id / status）も
    // 同じ経路（サーバ側の dto.Chat*Params が名前を検証する）。
    const { action, params } = splitConfirm(c);
    try {
      const r = await api.chatConfirm(action, params, { idempotencyKey: key });
      push({ kind: r.ok ? "ok" : "warn", message: r.message });
      // 結果はカードの位置に残す（承認した内容と結果を会話の中で読めるように）
      patch(id, (m) => ({
        ...m,
        confirm: undefined,
        result: { ok: r.ok, message: r.message, link: r.ok ? resultLink(action, r.detail ?? {}) : null },
      }));
    } catch (e) {
      // 403 / 422（E1003: 未対応の action・params 不正）は何度送っても同じなので、
      // カードを消してサーバの理由をその場に残す。ネットワーク断・5xx・429 だけ
      // カードを残して再試行できるようにする（同じ鍵で送るので二重実行にならない）
      const f = confirmFailure(action, e);
      push({ kind: "warn", message: f.message });
      if (!f.retryable) {
        patch(id, (m) => ({ ...m, confirm: undefined, result: { ok: false, message: f.message, link: null } }));
      }
    } finally {
      approvingRef.current = null;
      setApproving(null);
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
                        <span style={{ color: "var(--ink2)", flex: 1 }}>{msg.confirm.prompt ?? msg.confirm.action}</span>
                        {/* どのスキーマの新版になるかを承認前に見せる。prompt はエージェントが
                            書く文で対象を含むとは限らず、承認後にサーバは doc_type 無しを
                            invoice として扱う。対象が読めない提案は承認させない */}
                        {msg.confirm.action === "update_schema" && (
                          <span className="sub" title="承認するとこのスキーマの新しい版が作られます">
                            対象スキーマ: <b>{targetDocType(msg.confirm) ?? "（不明）"}</b>
                          </span>
                        )}
                        <button
                          className="btn sm primary"
                          disabled={
                            approving !== null ||
                            (msg.confirm.action === "update_schema" && !targetDocType(msg.confirm))
                          }
                          title={
                            msg.confirm.action === "update_schema" && !targetDocType(msg.confirm)
                              ? "対象のスキーマが特定できません。「<スキーマ名> のスキーマに…」のように言い直してください"
                              : undefined
                          }
                          onClick={() => approve(msg.id, msg.confirm!, msg.confirmKey)}
                        >
                          {approving === msg.id ? "実行中…" : "承認して実行"}
                        </button>
                        <button
                          className="btn sm ghost"
                          disabled={approving === msg.id}
                          onClick={() => patch(msg.id, (m) => ({ ...m, confirm: undefined }))}
                        >
                          今回はしない
                        </button>
                      </div>
                      {/* 実際にサーバへ送る内容（LLM の確認文とは別に、承認の根拠として見せる） */}
                      {describeConfirm(msg.confirm).length > 0 && (
                        <ul className="cc-body">
                          {describeConfirm(msg.confirm).map((line) => (
                            <li key={line}>{line}</li>
                          ))}
                        </ul>
                      )}
                    </div>
                  )}
                  {msg.result && (
                    <div className="tool-card" role="status">
                      <span>{msg.result.ok ? "✅" : "⚠️"}</span>
                      <span style={{ flex: 1 }}>{msg.result.message}</span>
                      {msg.result.link && (
                        <Link className="btn sm primary" href={msg.result.link.href}>
                          {msg.result.link.label}
                        </Link>
                      )}
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
