// スキーマ管理 → チャットの受け渡し（lib/schemaChat）。
//
// リンクが開いていたスキーマの doc_type を運び、依頼文がその doc_type を含むことを固定する。
// これが崩れると、delivery_note を開いていた管理者の依頼が invoice の新版になる
// （gateway の RuleBasedChatAgent は「<doc_type> のスキーマに」を読み、無ければ invoice）。

import { describe, expect, it } from "vitest";

import { chatHrefForSchema, schemaAddRequest } from "./schemaChat";

describe("chatHrefForSchema", () => {
  it("doc_type をクエリに載せる（URL エンコード込み）", () => {
    expect(chatHrefForSchema("delivery_note")).toBe("/chat?doc_type=delivery_note");
    expect(chatHrefForSchema("発注書")).toBe(`/chat?doc_type=${encodeURIComponent("発注書")}`);
    expect(chatHrefForSchema("a&b=c")).toBe("/chat?doc_type=a%26b%3Dc");
  });

  it("URL から読み戻すと元の doc_type になる", () => {
    const href = chatHrefForSchema("見積書 v2");
    const params = new URL(href, "http://localhost").searchParams;
    expect(params.get("doc_type")).toBe("見積書 v2");
  });
});

describe("schemaAddRequest", () => {
  it("doc_type 入りの依頼文（「<doc_type> のスキーマに「<項目>」を追加して」）", () => {
    expect(schemaAddRequest("delivery_note")).toBe("delivery_note のスキーマに「支払方法」を追加して");
    expect(schemaAddRequest("発注書", "担当者")).toBe("発注書 のスキーマに「担当者」を追加して");
  });

  it("doc_type が無い／空白だけなら対象を書かない従来の文", () => {
    expect(schemaAddRequest(null)).toBe("スキーマに「支払方法」を追加して");
    expect(schemaAddRequest(undefined)).toBe("スキーマに「支払方法」を追加して");
    expect(schemaAddRequest("   ")).toBe("スキーマに「支払方法」を追加して");
  });

  it("gateway の rule-based 解析（「<doc_type> のスキーマ」）と同じ形になる", () => {
    // services/gateway/src/newfan_gateway/chat.py の _doc_type_in と同じ正規表現
    const re = /(?:^|\s)([^\s「『」』、。]+?)\s*のスキーマ/;
    expect(schemaAddRequest("delivery_note").match(re)?.[1]).toBe("delivery_note");
    expect(schemaAddRequest("発注書").match(re)?.[1]).toBe("発注書");
    expect(schemaAddRequest(null).match(re)).toBeNull();
  });
});
