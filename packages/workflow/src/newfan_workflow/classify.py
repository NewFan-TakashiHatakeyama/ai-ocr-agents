"""帳票種別の内容ベース分類（⑦）。純ロジック（DB も HTTP も LLM も持たない）。

抽出前は「ファイル名」、抽出後は「OCRテキスト」を信号にして、候補 doc_type の中から
最も近い種別を決める決定論スコアリング。gateway は抽出UIのスキーマ自動サジェストに、
orchestrator は process.classify ゲートの実信号に使う。

スコアリングの原則（敵対的レビューで確定した3件の修正を含む）:
- 1出現 = 1証拠。重なり合う語（「見積」⊂「見積書」、"order"⊂"purchase order"）が同一
  箇所を多重加点しないよう、マッチ区間をマージした「領域」の数で数える。
- 英数字のみの語は語境界を要求する（"order" が "Border"/"recorder" に埋没ヒットしない）。
- 照合前に NFKC 正規化する（全角英数「ＩＮＶＯＩＣＥ」や半角カナ「ﾚｼｰﾄ」を取りこぼさない）。

確信が持てなければ doc_type=None を返し、呼び出し側が既定（スキーマ指定など）に
フォールバックできるようにする。LLM による精緻化は呼び出し側の任意（best-effort）。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache

# doc_type → 日本語/英語の代表語。ここに無い doc_type は doc_type 名そのものを語彙に使う。
# 別名（estimate=quotation, order=purchase_order 等）は独立キーにしない。独立させると
# 同じ日本語（例「発注書」）が複数キーに一致して同点になり、確信度が不当に下がる。
DOC_TYPE_SYNONYMS: dict[str, list[str]] = {
    "invoice": ["請求書", "御請求", "ご請求", "請求金額", "invoice"],
    "quotation": ["見積書", "御見積", "お見積", "見積", "見積金額", "quotation", "estimate"],
    "purchase_order": ["発注書", "注文書", "ご注文", "purchase order", "purchaseorder", "order"],
    "delivery_note": ["納品書", "納品", "delivery note", "deliverynote"],
    "receipt": ["領収書", "領収", "レシート", "receipt"],
    "statement": ["取引明細", "明細書", "statement"],
}

# 語彙として弱すぎる（どの帳票にも出る）語は無視する。
_STOPWORDS = {"合計", "金額", "日付", "no", "total", "amount", "date"}

_FILENAME_WEIGHT = 3.0  # ファイル名一致は本文一致より重い（命名は種別を強く示す）
_TEXT_WEIGHT = 1.0
_MAX_REGIONS = 3  # 同語の連呼で証拠が水増しされないよう飽和させる


@dataclass
class ClassifyCandidate:
    """分類候補。doc_type と、それを示す語彙（doc_type 名＋別名＋項目ラベル）。"""

    doc_type: str
    keywords: list[str] = field(default_factory=list)


@dataclass
class ClassifyOutcome:
    doc_type: str | None
    confidence: float  # 0..1
    reason: str
    scores: dict[str, float]


def _norm(s: str) -> str:
    # NFKC（全角英数→ASCII・半角カナ→全角カナ）→ 空白圧縮 → 英字小文字化。
    s = unicodedata.normalize("NFKC", s or "")
    return re.sub(r"\s+", " ", s).lower()


def canonical_doc_type(doc_type: str | None) -> str:
    """doc_type を正準名（DOC_TYPE_SYNONYMS のキー）へ解決する。

    「請求書」→ "invoice" のように、別名・日本語名を同一視するための正規化。
    ワークフローの許可リスト照合はこの正準名で行う（日本語名の doc_type が
    英語正準キーの分類結果に「別物」と誤判定されて halt しないように）。
    未知の名前はそのまま返す。
    """
    dt = _norm(doc_type or "").strip()
    if not dt:
        return ""
    for key, words in DOC_TYPE_SYNONYMS.items():
        if dt == key or any(dt == _norm(w) for w in words):
            return key
    return (doc_type or "").strip()


def synonyms_for(doc_type: str) -> list[str]:
    """doc_type の代表語彙。正準名へ解決してから別名一式を返す（双方向）。

    synonyms_for("請求書") が invoice の語彙一式を返すことで、日本語名スキーマの
    候補が英語正準キーの候補に語彙量で系統的に敗けることを防ぐ。
    """
    dt = (doc_type or "").strip()
    if not dt:
        return []
    words = [dt]
    canon = canonical_doc_type(dt)
    if canon in DOC_TYPE_SYNONYMS:
        words.append(canon)
        words += DOC_TYPE_SYNONYMS[canon]
    seen: set[str] = set()
    uniq: list[str] = []
    for w in words:
        k = _norm(w)
        if k and k not in seen:
            seen.add(k)
            uniq.append(w)
    return uniq


def build_candidate(doc_type: str, field_labels: list[str] | None = None) -> ClassifyCandidate:
    """スキーマ 1 件から候補を作る。語彙＝doc_type 別名＋項目ラベル（ストップワード除く）。"""
    kws = list(synonyms_for(doc_type))
    for lbl in field_labels or []:
        lbl = (lbl or "").strip()
        if lbl and lbl.lower() not in _STOPWORDS and len(lbl) >= 2:
            kws.append(lbl)
    seen: set[str] = set()
    uniq: list[str] = []
    for k in kws:
        kl = _norm(k)
        if kl and kl not in seen:
            seen.add(kl)
            uniq.append(k)
    return ClassifyCandidate(doc_type=doc_type, keywords=uniq)


@lru_cache(maxsize=1024)
def _kw_pattern(kw_norm: str) -> re.Pattern[str]:
    esc = re.escape(kw_norm)
    if kw_norm.isascii():
        # 英数語は語境界を要求（"order" が "border"/"recorder" に埋没ヒットしない）。
        # \b は '_' を語内文字と見なすため、英数字以外すべてを区切りとして扱う
        return re.compile(r"(?<![a-z0-9])" + esc + r"(?![a-z0-9])")
    return re.compile(esc)


def _match_regions(keywords_norm: list[str], haystack: str) -> tuple[int, list[str]]:
    """キーワード群が haystack 内で覆う「領域」数と、ヒットした語を返す。

    重なり合う語（「見積」⊂「見積書」）や包含別名（"order"⊂"purchase order"）が
    同一箇所を多重加点しないよう、マッチ区間をマージして 1領域=1証拠 で数える。
    """
    spans: list[tuple[int, int]] = []
    hits: list[str] = []
    for kw in keywords_norm:
        found = False
        for m in _kw_pattern(kw).finditer(haystack):
            spans.append(m.span())
            found = True
        if found:
            hits.append(kw)
    if not spans:
        return 0, []
    spans.sort()
    regions = 0
    cur_end = -1
    for s, e in spans:
        if s >= cur_end:
            regions += 1
            cur_end = e
        else:
            cur_end = max(cur_end, e)
    return min(regions, _MAX_REGIONS), hits


def classify_text(
    *,
    text: str,
    filename: str,
    candidates: list[ClassifyCandidate],
    min_confidence: float = 0.0,
) -> ClassifyOutcome:
    """ファイル名＋本文から最も近い doc_type を決める。

    確信度が min_confidence 未満なら doc_type=None を返す（呼び出し側が既定に倒す）。
    """
    fn = _norm(filename)
    tx = _norm(text)
    scores: dict[str, float] = {}
    matched: dict[str, list[str]] = {}
    for cand in candidates:
        norm_to_orig: dict[str, str] = {}
        for k in cand.keywords:
            kn = _norm(k)
            if kn and kn not in norm_to_orig:
                norm_to_orig[kn] = k
        kws = list(norm_to_orig.keys())
        fn_regions, fn_hits = _match_regions(kws, fn)
        tx_regions, tx_hits = _match_regions(kws, tx)
        scores[cand.doc_type] = fn_regions * _FILENAME_WEIGHT + tx_regions * _TEXT_WEIGHT
        hits: list[str] = []
        for h in fn_hits + tx_hits:
            orig = norm_to_orig.get(h, h)
            if orig not in hits:
                hits.append(orig)
        matched[cand.doc_type] = hits

    if not scores or max(scores.values()) <= 0:
        return ClassifyOutcome(doc_type=None, confidence=0.0, reason="手がかりが見つかりませんでした。", scores=scores)

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    top_dt, top_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else 0.0

    margin = top_score / (top_score + second_score) if (top_score + second_score) > 0 else 1.0
    evidence = min(1.0, top_score / (_FILENAME_WEIGHT))  # ファイル名1語 or 本文3語で飽和
    confidence = round(margin * (0.5 + 0.5 * evidence), 3)

    if confidence < min_confidence:
        return ClassifyOutcome(doc_type=None, confidence=confidence, reason="確信が持てませんでした。", scores=scores)

    hits = matched.get(top_dt, [])
    where = "ファイル名/本文"
    reason = f"「{'・'.join(hits[:3])}」が{where}に一致" if hits else "内容が最も近い"
    return ClassifyOutcome(doc_type=top_dt, confidence=confidence, reason=reason, scores=scores)
