// 記入例語の例示値の警告（設計 region-field-add-and-hint-v2 §2.5）。
//
// 判定そのものはサーバ（newfan_schemas.is_placeholder_example）にあり、ここでは書かない。
// 固定したいのは画面側の 3 点:
//   1. 問い合わせる値の選び方（未判定だけ・重複なし・空は送らない）
//   2. 応答の取り込み（送った順に対応づける。件数がずれたら何も足さない）
//   3. 保存後の通知の文言（表示名で出す・多いときは件数に畳む・無ければ出さない）

import { describe, expect, it } from "vitest";

import {
  PLACEHOLDER_ROW_NOTE,
  isPlaceholderExample,
  mergeVerdicts,
  placeholderSavedMessage,
  unknownExampleValues,
} from "./placeholderExample";
import type { SchemaSaveWarning } from "./types";

describe("unknownExampleValues", () => {
  it("未判定の例示値だけを出現順に、重複なしで返す", () => {
    const verdicts = new Map([["株式会社千曲川ホーム", false]]);
    expect(
      unknownExampleValues(
        ["〇〇株式会社", "株式会社千曲川ホーム", "YYYY/MM/DD", "〇〇株式会社"],
        verdicts,
      ),
    ).toEqual(["〇〇株式会社", "YYYY/MM/DD"]);
  });

  it("null / undefined / 空白だけの値は問い合わせない（例示値なしと同じ）", () => {
    expect(unknownExampleValues([null, undefined, "", "  "], new Map())).toEqual([]);
  });

  it("すべて判定済みなら空（同じ値を 2 度問い合わせない）", () => {
    const verdicts = new Map([
      ["〇〇株式会社", true],
      ["395,217", false],
    ]);
    expect(unknownExampleValues(["〇〇株式会社", "395,217"], verdicts)).toEqual([]);
  });
});

describe("mergeVerdicts", () => {
  it("送った順に応答を対応づけ、元の Map は変えない", () => {
    const prev = new Map([["395,217", false]]);
    const next = mergeVerdicts(prev, ["〇〇株式会社", "株式会社千曲川ホーム"], {
      items: [
        { value: "〇〇株式会社", placeholder: true },
        { value: "株式会社千曲川ホーム", placeholder: false },
      ],
    });
    expect(next.get("〇〇株式会社")).toBe(true);
    expect(next.get("株式会社千曲川ホーム")).toBe(false);
    expect(next.get("395,217")).toBe(false);
    expect(prev.size).toBe(1);
  });

  it("件数がずれた応答は取り込まない（別の値に注記を付けない）", () => {
    const next = mergeVerdicts(new Map(), ["〇〇株式会社", "YYYY/MM/DD"], {
      items: [{ value: "〇〇株式会社", placeholder: true }],
    });
    expect(next.size).toBe(0);
  });
});

describe("isPlaceholderExample", () => {
  const verdicts = new Map([
    ["〇〇株式会社", true],
    ["株式会社千曲川ホーム", false],
  ]);

  it("判定済みで記入例語のときだけ true", () => {
    expect(isPlaceholderExample("〇〇株式会社", verdicts)).toBe(true);
    expect(isPlaceholderExample("株式会社千曲川ホーム", verdicts)).toBe(false);
  });

  it("未判定・例示値なし（消した）は注記を出さない", () => {
    expect(isPlaceholderExample("YYYY/MM/DD", verdicts)).toBe(false);
    expect(isPlaceholderExample(null, verdicts)).toBe(false);
    expect(isPlaceholderExample(undefined, verdicts)).toBe(false);
  });

  it("行の注記は「例示値を消す」を案内する", () => {
    expect(PLACEHOLDER_ROW_NOTE).toContain("記入例の語のようです");
    expect(PLACEHOLDER_ROW_NOTE).toContain("例示値を消す");
  });
});

describe("placeholderSavedMessage", () => {
  const w = (field: string, example_value: string): SchemaSaveWarning => ({
    code: "placeholder_example",
    field,
    example_value,
  });

  it("警告が無ければ通知しない", () => {
    expect(placeholderSavedMessage([])).toBeNull();
    expect(placeholderSavedMessage(undefined)).toBeNull();
    expect(placeholderSavedMessage(null)).toBeNull();
  });

  it("表示名と例示値で並べ、位置のヒントが使われないことと直し方を伝える", () => {
    const labels: Record<string, string> = { issuer_name: "発行元" };
    const msg = placeholderSavedMessage(
      [w("issuer_name", "自社名(ロゴや社判も登録できます)"), w("issue_date", "YYYY/MM/DD")],
      (f) => labels[f],
    );
    expect(msg).toContain("読取領域 2 件");
    // 表示名が無い項目は項目名のまま
    expect(msg).toContain("発行元「自社名(ロゴや社判も登録できます)」、issue_date「YYYY/MM/DD」");
    expect(msg).toContain("位置のヒントが使われません");
    expect(msg).toContain("例示値を消す");
  });

  it("多いときは 3 件まで並べて残りは件数、長い例示値は切る", () => {
    const long = "東京都千代田区住所1住所2ビル名等東京都千代田区住所1住所2ビル名等";
    const msg = placeholderSavedMessage([
      w("a", long),
      w("b", "〇〇株式会社"),
      w("c", "〒000-0000"),
      w("d", "YYYY/MM/DD"),
      w("e", "記入例"),
    ])!;
    expect(msg).toContain("読取領域 5 件");
    expect(msg).toContain(" ほか 2 件");
    expect(msg).not.toContain("d「");
    expect(msg).toContain(`a「${Array.from(long).slice(0, 24).join("")}…」`);
  });
});
