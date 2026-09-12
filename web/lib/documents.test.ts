// 帳票一覧の表示名と検索（lib/documents）。
//
// 一覧・レビューキュー・検証画面が同じ規則で名前を出し、検索が原本ファイル名にも
// 当たることを固定する（以前は ID・種別・external_ref にしか当たらなかった）。

import { describe, expect, it } from "vitest";

import { documentDisplayName, matchesDocumentQuery } from "./documents";

const named = {
  document_id: "doc_0123456789abcdef",
  original_name: "請求書_2026-09_ACME.pdf",
  doc_type: "invoice",
  external_ref: "ERP-1",
};
const unnamed = {
  document_id: "doc_noname",
  original_name: null,
  doc_type: null,
  external_ref: null,
};

describe("documentDisplayName", () => {
  it("原本ファイル名があればそれを出す", () => {
    expect(documentDisplayName(named)).toBe("請求書_2026-09_ACME.pdf");
  });

  it("名前が無い／空白だけなら document_id に落とす", () => {
    expect(documentDisplayName(unnamed)).toBe("doc_noname");
    expect(documentDisplayName({ document_id: "doc_x", original_name: "   " })).toBe("doc_x");
    expect(documentDisplayName({ document_id: "doc_y" })).toBe("doc_y");
  });
});

describe("matchesDocumentQuery", () => {
  it("空のキーワードは全件に一致する", () => {
    expect(matchesDocumentQuery(named, "")).toBe(true);
    expect(matchesDocumentQuery(unnamed, "   ")).toBe(true);
  });

  it("原本ファイル名の部分一致（大文字小文字を区別しない）", () => {
    expect(matchesDocumentQuery(named, "acme")).toBe(true);
    expect(matchesDocumentQuery(named, "請求書")).toBe(true);
    expect(matchesDocumentQuery(named, "2026-09")).toBe(true);
  });

  it("従来どおり ID・種別・external_ref にも当たる", () => {
    expect(matchesDocumentQuery(named, "0123456789")).toBe(true);
    expect(matchesDocumentQuery(named, "INVOICE")).toBe(true);
    expect(matchesDocumentQuery(named, "erp-1")).toBe(true);
  });

  it("どこにも含まれなければ false。名前の無い行でも落ちない", () => {
    expect(matchesDocumentQuery(named, "納品書")).toBe(false);
    expect(matchesDocumentQuery(unnamed, "noname")).toBe(true);
    expect(matchesDocumentQuery(unnamed, "acme")).toBe(false);
  });
});
