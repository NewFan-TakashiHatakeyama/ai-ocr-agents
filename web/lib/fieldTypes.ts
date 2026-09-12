// 正規化型（§5.6 FieldType）の選択肢と、抽出値の見た目からの型推測。
//
// 選択肢は **テンプレート化プレビューとスキーマ管理画面の 2 か所**で使う。片方だけに
// 持つと、型を足したとき（ADR-0007 の address_jp、jp_bank_account）にもう片方が
// 生の値（"address_jp"）を並べたり選択肢を欠いたりする。値は gateway が
// newfan_schemas.FieldType で検証する（無い型は E1003）ので、ここに足す型は
// FieldType にもあること。

export const TYPE_OPTIONS = [
  ["string", "文字列"],
  ["date", "日付"],
  ["money_jpy", "金額(円)"],
  ["number", "数値"],
  ["tax_rate_jp", "税率"],
  ["jp_invoice_reg_no", "登録番号(T+13桁)"],
  ["jp_bank_account", "銀行口座"],
  // 住所（ADR-0007）: 郵便番号・見出し語を落とし、都道府県〜建物名/階を値にする
  ["address_jp", "住所(日本)"],
  // table 型は新規作成では選べないが、編集モードで既存の明細定義が来たときに
  // select が値を表示できずに壊れるため選択肢としては持つ（当該行は読み取り専用）。
  ["table", "明細（表）"],
] as const;

// 抽出値の見た目から正規化型を推測する。
//
// あくまで初期値で、テンプレート化の画面で利用者が直せる。迷ったら string に倒す
// （誤った型はそのまま保存され、次回抽出の正規化が値を壊す）。
//
// TemplatizeSchema から切り出した。領域指定プレビューでも同じ推測を使うため、
// コンポーネントではなく lib に置く。

export function guessFieldType(value: string | null | undefined): string {
  if (!value) return "string";
  const t = value.trim();
  if (/^T\d{13}$/.test(t)) return "jp_invoice_reg_no";
  if (/^\d{1,2}(\.\d+)?\s?[%％]$/.test(t)) return "tax_rate_jp";
  // money は「値全体が金額の形」の時だけ。部分一致 /円/ にすると
  // 「円谷プロダクション株式会社」「渋谷区円山町」まで money_jpy になり、
  // そのまま保存されると次回抽出の正規化が値を破壊する（敵対的レビュー確定）
  if (/^[¥￥]?\s?-?\d{1,3}(,\d{3})*(\.\d+)?\s?円?$/.test(t) && /[¥￥,円]/.test(t))
    return "money_jpy";
  if (/^\d{4}[-/年.]\s?\d{1,2}([-/月.]\s?\d{1,2}\s?日?)?$/.test(t)) return "date";
  if (/^-?\d+(\.\d+)?$/.test(t.replace(/,/g, ""))) return "number";
  return "string";
}
