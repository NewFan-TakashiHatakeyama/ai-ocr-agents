"""読取領域ヒントの候補解決・事前ガード・種類判定（設計 region-field-add-and-hint-v2 §2.5）。

純関数のみ。langgraph にも DB にも依存しない。schema の走査と metrics への記録は
llm_nodes 側（``build_region_hints`` / ``make_kie_extract``）。

設計上の要点:

- ヒントは矩形ではなく **矩形の中にある span の候補列（id と原文）** として渡す（D13）。
  モデルが実際に選ぶのは span なので、候補を名指しすれば座標は要らない。
- **渡す前に決定論で落とす**（D14）。候補 0 件・型付き項目で型に合わない・例示値と
  候補の「種類」が衝突する、のいずれかならヒントごと落とし、理由を metrics に残す。
- **判定の非対称性**: 誤って落としてもその項目は「ヒント無し＝対照の挙動」に戻るだけで
  対照より悪くはならない。誤って通した害（隣の行の「大熊邸」で正解を壊す）とは
  釣り合わないので、**迷ったら落とす**側に倒す。分類器は保守的でよく、落とした件数と
  その項目の正解率はゲート（設計 §3）で測る。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Optional

from newfan_normalizers import NormContext, normalize
from newfan_paddle_client.spans import overlap_area
from newfan_schemas import FieldType, Span, norm_key

# 記入例語の判定は gateway と共有する（下の「記入例語」節）。``as`` は明示的な re-export
# （mypy strict の no_implicit_reexport で ``from region_hint import`` が通るように）
from newfan_schemas import is_placeholder_example as is_placeholder_example

# span を候補とみなす閾値: span の中心点が矩形内、**または**重なりが span 面積のこの割合以上。
# 除外領域の EXCLUDE_SPAN_RATIO（0.5）は **流用しない**。除外は「消しすぎ」が危険なので
# 厳しめが正しいが、ヒントは逆で「拾い漏れ」が危険 ── 人が手で引いた枠は文字の一部にしか
# 掛からないことが普通で、0 件になるとヒントごと落ちる（設計 §2.5 / R5）。
HINT_SPAN_RATIO = 0.3

# プロンプトへ載せる候補の上限。切るときは読み順ではなく **矩形との重なりが大きい順**
# （読み順で切ると、大きな枠で本命が後ろに来たとき落ちる。R17）。切った件数は metrics へ。
HINT_MAX_CANDIDATES = 12

# 型付き項目: 候補（連結にも各候補にも）にその型として解釈できるものが無ければ落とす。
TYPED_FIELD_TYPES: frozenset[str] = frozenset(
    {
        FieldType.MONEY_JPY.value,
        FieldType.DATE.value,
        FieldType.NUMBER.value,
        FieldType.TAX_RATE_JP.value,
        FieldType.JP_INVOICE_REG_NO.value,
        FieldType.JP_BANK_ACCOUNT.value,
    }
)

# 落とす理由（metrics.region.hints.dropped の値）
REASON_PAGE_OUT_OF_RANGE = "page_out_of_range"
REASON_PAGE_UNPROJECTABLE = "page_unprojectable"
REASON_NO_SPANS = "no_spans_in_region"
REASON_OVERLAPS_EXCLUDE = "overlaps_exclude"
REASON_TYPE_MISMATCH = "type_mismatch"
REASON_KIND_CONFLICT = "kind_conflict"
REASON_PLACEHOLDER_EXAMPLE = "placeholder_example"

BBox = list[int]


# ---------------- 候補の判定と順位 ----------------


def span_inside(bbox: Optional[list[int]], rect: BBox) -> bool:
    """span が矩形の候補か。中心点が矩形内、または重なりが span 面積の HINT_SPAN_RATIO 以上。"""
    if not bbox or len(bbox) < 4 or not rect or len(rect) < 4:
        return False
    cx, cy = (bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0
    if rect[0] <= cx <= rect[2] and rect[1] <= cy <= rect[3]:
        return True
    area = float((bbox[2] - bbox[0]) * (bbox[3] - bbox[1]))
    if area <= 0:
        # 退化 bbox は中心点判定だけで決める（ゼロ除算を避ける）
        return False
    return overlap_area(list(bbox), list(rect)) / area >= HINT_SPAN_RATIO


def rank_candidates(spans: list[Span], rect: BBox) -> list[Span]:
    """矩形との重なり面積が大きい順。同点は読み順（span_id）。"""
    return sorted(
        spans,
        key=lambda s: (-overlap_area(list(s.bbox), list(rect)) if s.bbox else 0.0, s.span_id),
    )


def rect_overlaps_any(rect: BBox, others: list[BBox]) -> bool:
    return any(overlap_area(list(rect), list(o)) > 0 for o in others if o and len(o) >= 4)


# ---------------- 型の解釈（決定論正規化のパーサをそのまま使う） ----------------

_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_REG_NO = re.compile(r"T[0-9A-Z]{13}")
_HAS_DIGIT = re.compile(r"\d")
# 文脈年は渡さない。年を省いた「5月31日」は落ちる側に倒れる（ヒント無し＝対照の挙動）。
# 文脈年を渡すと '10.5' '3-1' '12/31' のような数量・比率・分数まで日付として通る
# （敵対的レビュー Phase B）。非対称性の原則: 迷ったら落とす。
_PROBE_CTX = NormContext()
_NUMERIC_TYPES = frozenset({FieldType.MONEY_JPY, FieldType.NUMBER, FieldType.TAX_RATE_JP})


def interpretable_as(field_type: str, text: str) -> bool:
    """text を field_type として解釈できるか。

    §5.6 の正規化器をそのまま使う（新しくパーサを書かない）。ただし正規化器の
    「解釈できなかった」の表し方は型で違う:

    - money_jpy / number / tax_rate_jp / jp_bank_account: ``value is None``
    - date: 解釈できなければ**入力をそのまま返す**（None にならない）。解釈できた
      ときは必ず ISO 形式になるので、出力の形で判定する
    - jp_invoice_reg_no: 常に ``"T" + 本体`` を返す（None にならない）。本体が 13 桁
      （混同文字を含み得るので英数字）である形で判定する
    """
    try:
        ftype = FieldType(field_type)
    except ValueError:
        return True  # 未知の型は判定しない（落とす根拠が無い）
    # 数値系は**数字を 1 つも含まなければ解釈できない**と先に決める。norm_money_jpy は
    # 本文に '.' があると小数点あいまい扱いで入力をそのまま value に返すため、OCR の
    # 点線リーダー「………」（NFKC で '...'）や「No.」「Co., Ltd.」が金額として通っていた
    # （敵対的レビュー Phase B の major）。正規化器の返し方に依存せず、ここで止める。
    if ftype in _NUMERIC_TYPES and not _HAS_DIGIT.search(text):
        return False
    res = normalize(ftype, text, _PROBE_CTX)
    if res.value is None:
        return False
    if ftype in _NUMERIC_TYPES and not _HAS_DIGIT.search(res.value):
        return False
    if ftype is FieldType.DATE:
        return bool(_ISO_DATE.fullmatch(res.value))
    if ftype is FieldType.JP_INVOICE_REG_NO:
        return bool(_REG_NO.fullmatch(res.value))
    return True


# ---------------- 種類判定（kind_conflict） ----------------

# §2.5 の表を順に当てて、最初に当たった種類にする。company を address より先に
# 評価するのは、社名に「町」「市」が入る例（株式会社町田製作所）があるため。
_KIND_AMOUNT = re.compile(r"[-△▲]?¥?\d[\d,]*(?:\.\d+)?円?-?")
_KIND_DATE = re.compile(
    r"(?:令和|平成|昭和|大正|明治|[RHSTM])\s*(?:元|\d{1,2})\s*年"
    r"|\d{4}\s*年\s*\d{1,2}\s*月"
    # 区切り形は年月日の 3 つ揃いを要求する（2 つでは「INV-2024-001」の伝票番号が日付になる）
    r"|\d{4}\s*[/.\-]\s*\d{1,2}\s*[/.\-]\s*\d{1,2}"
    r"|\d{1,2}\s*月\s*\d{1,2}\s*日"
)
_KIND_ID = re.compile(r"(?=.*\d)[A-Za-z0-9\-]+")
_KIND_COMPANY_JA = ("株式会社", "有限会社", "合同会社", "合資会社", "合名会社", "(株)", "(有)")
_KIND_COMPANY_EN = re.compile(r"\b(?:co\.|inc\b\.?|ltd\b\.?|corp\b\.?|llc\b|k\.k\.)", re.IGNORECASE)
_KIND_PREF = re.compile(r"北海道|東京都|京都府|大阪府|[一-鿿]{2,3}県")
_KIND_ADDRESS_WORD = re.compile(r"丁目|番地|〒")
_KIND_LOCALITY = re.compile(r"[市区町村郡]")
_KIND_BUILDING = re.compile(r"(?:邸|ビル|マンション|ハイツ|荘|館)$")
_KANJI = r"[一-鿿々〆ヶ]"
# 姓と名の**間に空白があるもの**だけを人名とみなす。空白無し（「請求書」「発行日」「合計金額」
# 「山田工務店」）まで person にすると、見出し語が全部 person になり、日付・金額の例示値と
# 衝突して分割日付を落とす（敵対的レビュー Phase B の major）。空白の無い人名は
# unknown に落ちるが、unknown は衝突にしないので効き目は失わない。
_KIND_PERSON = re.compile(rf"{_KANJI}{{1,4}} {_KANJI}{{1,3}}")


def classify_kind(text: Optional[str]) -> str:
    """「種類」を返す: amount / date / id / company / address / building / person / unknown。

    小さな規則ベースの分類器。精度より**保守性と予測可能性**を優先する（誤って落としても
    対照に戻るだけ。誤って通す側に倒さない）。
    """
    if text is None:
        return "unknown"
    # NFKC で全角英数・全角スペース・㈱ を揃え、空白の連続は 1 つにする（person の
    # 「姓 名」判定は空白 1 つを許す）
    t = " ".join(unicodedata.normalize("NFKC", str(text)).split())
    if not t:
        return "unknown"
    compact = t.replace(" ", "")
    if _KIND_AMOUNT.fullmatch(compact):
        return "amount"
    if _KIND_DATE.search(compact):
        return "date"
    if _KIND_ID.fullmatch(compact):
        return "id"
    if any(k in compact for k in _KIND_COMPANY_JA) or _KIND_COMPANY_EN.search(t):
        return "company"
    if (
        _KIND_PREF.search(compact)
        or _KIND_ADDRESS_WORD.search(compact)
        # 市区町村郡の 1 文字だけでは人名（中村・町田・市川）を巻き込むので、
        # 番地相当の数字を伴うときだけ住所とみなす
        or (_KIND_LOCALITY.search(compact) and any(ch.isdigit() for ch in compact))
    ):
        return "address"
    if _KIND_BUILDING.search(compact):
        return "building"
    if compact.endswith("様") or _KIND_PERSON.fullmatch(t):
        return "person"
    return "unknown"


# 衝突とみなす組（例示値の種類 → 候補の種類）。company ↔ person と、unknown を含む組は
# 衝突にしない（区別が難しく、誤って落とすと効き目を失う側に倒れる）。
_ALL_KINDS = frozenset({"amount", "date", "id", "company", "address", "building", "person"})
KIND_CONFLICTS: dict[str, frozenset[str]] = {
    "company": frozenset({"address", "building", "amount", "date", "id"}),
    "person": frozenset({"address", "building", "amount", "date", "id"}),
    "amount": _ALL_KINDS - {"amount"},
    "date": _ALL_KINDS - {"date"},
}


def kinds_conflict(example_value: Optional[str], candidate_texts: list[str]) -> bool:
    """例示値と候補の種類が衝突するか。

    候補は **1 span ずつ**と**読み順で連結したもの**の両方を分類する。type_mismatch と同じ
    理由で、日付は「令和」「5年」「5月」「1日」に割れるため、単体では date にならない。
    候補（単体または連結）に例示値と同じ種類が 1 つでもあれば衝突なし。
    """
    if not example_value:
        return False
    ex_kind = classify_kind(example_value)
    conflicts = KIND_CONFLICTS.get(ex_kind)
    if not conflicts:
        return False
    texts = list(candidate_texts)
    if len(texts) > 1:
        texts.append(" ".join(candidate_texts))
    cand_kinds = {classify_kind(t) for t in texts}
    if ex_kind in cand_kinds:
        return False
    return bool(cand_kinds & conflicts)


# ---------------- 記入例語（placeholder_example） ----------------

# 判定 ``is_placeholder_example`` は newfan_schemas（``placeholder.py``）にある。gateway が
# テンプレート化／領域編集画面の保存時の警告に**同じ判定**を使うため、共有パッケージへ
# 判定を変えずに移した（規則 4 つと、採らないと決めた語の経緯もそちらのコメント）。
# ここでは先頭で import して prevalidate で使い、``region_hint.is_placeholder_example``
# の参照（region_mask・既存テスト）が壊れないよう明示的に re-export している。


# ---------------- 事前ガード ----------------


def prevalidate(
    field: dict[str, Any],
    cands: list[Span],
    example_value: Optional[str],
    region_px: BBox,
    exclude_px: list[BBox],
) -> Optional[str]:
    """落とす理由を返す（通すなら None）。順に評価する。

    - placeholder_example: 例示値が記入例語（「自社名」「住所1」）。テンプレート元が記入例の
      帳票で、種類判定が効かないまま別雛形の候補に従ってしまう（sample2 ← sample13）。
      テンプレート側の欠陥なので、この帳票の事情（候補の有無・型）より先に伝える
    - no_spans_in_region: 候補 0 件。この帳票ではその位置に何も無い（レイアウト違い）
    - overlaps_exclude: 候補 0 件で、かつ矩形が適用済み除外領域と重なる。作者が矛盾した
      領域を引いている。no_spans と区別して伝えるためだけにある（候補は除外適用後の
      span から選ぶので、除外と重なる読取領域は自然に 0 件になる）
    - type_mismatch: 型付き項目で、候補の**連結**にも各候補にもその型として解釈できる
      ものが無い。連結を見るのは日付が「令和」「5年」「5月」「1日」に割れるため
      （単体で見ると全部落ちる。R6）
    - kind_conflict: 例示値と候補の種類が衝突する（sample8: 会社名の項目に建物名）
    """
    if is_placeholder_example(example_value):
        return REASON_PLACEHOLDER_EXAMPLE
    if not cands:
        if rect_overlaps_any(region_px, exclude_px):
            return REASON_OVERLAPS_EXCLUDE
        return REASON_NO_SPANS
    ftype = str(field.get("type") or "")
    if ftype in TYPED_FIELD_TYPES:
        in_reading_order = sorted(cands, key=lambda s: s.span_id)
        joined = " ".join(s.text for s in in_reading_order)
        if not (
            interpretable_as(ftype, joined) or any(interpretable_as(ftype, s.text) for s in cands)
        ):
            return REASON_TYPE_MISMATCH
    # 連結の判定（kinds_conflict / example_present）は読み順で渡す
    if kinds_conflict(example_value, [s.text for s in sorted(cands, key=lambda x: x.span_id)]):
        return REASON_KIND_CONFLICT
    return None


def example_present(example_value: Optional[str], cands: list[Span]) -> bool:
    """同じ原文がこの帳票の同じ位置にもあるか（照合キーは D18 の norm_key）。"""
    if not example_value:
        return False
    key = norm_key(example_value)
    if not key:
        return False
    if any(norm_key(s.text) == key for s in cands):
        return True
    # 手描きの例示値は「枠の下の span を読み順で連結したもの」（D11）なので、
    # 候補 1 つずつと比べると同じ紙面でも一致しない。連結にも当てる。
    joined = norm_key("".join(s.text for s in sorted(cands, key=lambda x: x.span_id)))
    return bool(joined) and key in joined


# 従った／捨てた（D15）の集合演算は llm_adapter の kie.py（``hint_outcome``）にある。
# 戻り値 KieResult.hint_outcomes を make_kie_extract が metrics.region.hints.outcomes へ写す。
