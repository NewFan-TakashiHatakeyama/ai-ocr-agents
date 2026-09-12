// 取込形式の定義（lib/uploads）が ingest の受理形式と食い違わないことを固定する。
//
// 正は services/ingest/src/newfan_ingest/validation.py の _EXT_TO_KIND から
// Office（E1003 で必ず拒否）を除いたもの。ここが変わるときは validation.py も
// 一緒に変わっているはず。

import { describe, expect, it } from "vitest";

import {
  ACCEPTED_UPLOAD_EXTENSIONS,
  ACCEPTED_UPLOAD_TYPES,
  UPLOAD_ACCEPT,
  UPLOAD_FORMATS_HINT,
  UPLOAD_FORMATS_LABEL,
} from "./uploads";

describe("ACCEPTED_UPLOAD_TYPES", () => {
  it("ingest が受理する形式（PDF / PNG / JPEG / TIFF）と一致する", () => {
    expect(ACCEPTED_UPLOAD_TYPES.map((t) => t.mime)).toEqual([
      "application/pdf",
      "image/png",
      "image/jpeg",
      "image/tiff",
    ]);
    expect([...ACCEPTED_UPLOAD_EXTENSIONS].sort()).toEqual(
      [".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"].sort(),
    );
  });

  it("Office（docx/xlsx/pptx）を含まない（ingest は E1003 で拒否する）", () => {
    for (const ext of [".docx", ".xlsx", ".pptx"]) {
      expect(ACCEPTED_UPLOAD_EXTENSIONS).not.toContain(ext);
    }
    expect(UPLOAD_ACCEPT).not.toMatch(/officedocument|msword|ms-excel/);
  });

  it("拡張子はドット始まりの小文字（ingest の照合と同じ綴り）", () => {
    for (const ext of ACCEPTED_UPLOAD_EXTENSIONS) {
      expect(ext).toMatch(/^\.[a-z0-9]+$/);
    }
  });
});

describe("UPLOAD_ACCEPT", () => {
  it("MIME と拡張子の両方を並べる（片方しか持たないブラウザ/OS がある）", () => {
    const parts = UPLOAD_ACCEPT.split(",");
    for (const t of ACCEPTED_UPLOAD_TYPES) {
      expect(parts).toContain(t.mime);
      for (const ext of t.extensions) expect(parts).toContain(ext);
    }
    expect(parts).not.toContain("");
  });
});

describe("UPLOAD_FORMATS_LABEL / HINT", () => {
  it("利用者向けの文言は対応形式だけを挙げ、Word/Excel を謳わない", () => {
    expect(UPLOAD_FORMATS_LABEL).toBe("PDF / PNG / JPEG / TIFF");
    expect(UPLOAD_FORMATS_HINT).toContain(UPLOAD_FORMATS_LABEL);
    expect(UPLOAD_FORMATS_HINT).not.toMatch(/Word|Excel|Office/);
  });
});
