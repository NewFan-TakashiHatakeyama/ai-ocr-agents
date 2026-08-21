// 冪等キー用の UUID 生成。
//
// crypto.randomUUID() は **secure context 専用**（https / localhost のみ）。
// 本番 ALB は HTTP 配信のため（ADR-0004: HTTPS は ACM 証明書待ちで保留）、
// ブラウザでは undefined になり、呼ぶと TypeError で処理全体が落ちる。
// 実際に「抽出を開始」が毎回トーストだけ出して何も起きない事象になっていた。
// secure context ではネイティブ実装を使い、それ以外は getRandomValues（非 secure でも
// 使える）→ Math.random の順にフォールバックする。
export function newUuid(): string {
  const c: Crypto | undefined = typeof crypto !== "undefined" ? crypto : undefined;
  if (c && typeof c.randomUUID === "function") return c.randomUUID();
  const bytes = new Uint8Array(16);
  if (c && typeof c.getRandomValues === "function") {
    c.getRandomValues(bytes);
  } else {
    for (let i = 0; i < 16; i += 1) bytes[i] = Math.floor(Math.random() * 256);
  }
  // RFC 4122 v4 のビットを立てる
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
