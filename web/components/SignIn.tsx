"use client";

import { useState } from "react";

// トークン投入画面（§6.1）。
//
// これまで web にはトークンを入れる導線が無く、DevTools で
// localStorage.setItem("nf_token", ...) を手打ちする必要があった。
// JWT をイメージに焼くと公開 JS バンドルに入り、ALB は公開されているため
// URL を知る誰もが API を叩けてしまうので、焼き込みではなくこの画面で受け取る。
//
// 本来は ID/パスワードでログインして gateway がトークンを発行する形にすべきだが、
// gateway に認証エンドポイントが無い（§6.1 は JWT/API キーを外部発行の前提）。
// ここは「発行済みトークンを貼る」までを担う。

export function SignIn({ onSignedIn }: { onSignedIn: () => void }) {
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const t = token.trim();
    if (!t) return;
    setBusy(true);
    setError(null);
    try {
      // 貼られた文字列が本当に通るか、保存前に API で確かめる。
      // 保存してから 403 が続くと、利用者には原因が分からない。
      const base = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000/v1";
      const r = await fetch(`${base}/documents?limit=1`, {
        headers: { Authorization: `Bearer ${t}` },
      });
      if (r.status === 401 || r.status === 403) {
        setError("このトークンでは認証できませんでした。発行し直してください。");
        return;
      }
      if (!r.ok) {
        setError(`API がエラーを返しました（${r.status}）。時間をおいて再試行してください。`);
        return;
      }
      window.localStorage.setItem("nf_token", t);
      onSignedIn();
    } catch {
      setError("API に接続できませんでした。ネットワークを確認してください。");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="signin">
      <form className="signin-card" onSubmit={submit}>
        {/* 見出しはロゴが担う。h1 のまま画像を入れて、名前は alt に持たせる
            （画像が出ない環境やスクリーンリーダにサービス名が残る）。 */}
        <h1 className="signin-logo">
          <img src="/logo-full.png" alt="NewFan AI OCR" width={908} height={151} />
        </h1>
        <p className="signin-lead">アクセストークンを貼り付けてください。</p>
        <textarea
          value={token}
          onChange={(e) => setToken(e.target.value)}
          placeholder="ここに JWT を貼り付け（xxxxx.yyyyy.zzzzz）"
          rows={4}
          spellCheck={false}
          aria-label="アクセストークン"
        />
        {error && <p className="signin-error">{error}</p>}
        <button type="submit" disabled={busy || !token.trim()}>
          {busy ? "確認中…" : "サインイン"}
        </button>
        <p className="signin-hint">
          トークンは <code>scripts/aws_env.sh token</code> で発行できます。
          この端末のブラウザにのみ保存されます。
        </p>
      </form>
    </div>
  );
}
