// 抽出値の見た目から正規化型（§5.6 FieldType）を推測する。
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
