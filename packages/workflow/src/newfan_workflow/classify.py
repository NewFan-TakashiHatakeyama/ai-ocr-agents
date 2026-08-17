"""帳票種別の内容ベース分類（⑦）。純ロジック（DB も HTTP も LLM も持たない）。

抽出前は「ファイル名」、抽出後は「OCRテキスト（layout_markdown）」を信号にして、
候補 doc_type の中から最も近い種別を決める決定論スコアリング。gateway は抽出UIの
スキーマ自動サジェストに、orchestrator は process.classify ゲートの実信号に使う。

分類そのものは軽量（キーワード一致）で、閾値以上の確信度が出たときだけ採用する。
確信が持てなければ doc_type=None を返し、呼び出し側が既定（スキーマ指定など）に
フォールバックできるようにする。LLM による精緻化は呼び出し側の任意（best-effort）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# doc_type → 日本語/英語の代表語。ここに無い doc_type は doc_type 名そのものを語彙に使う。
DOC_TYPE_SYNONYMS: dict[str, list[str]] = {
    "invoice": ["請求書", "御請求", "ご請求", "請求金額", "invoice"],
    "quotation": ["見積書", "御見積", "お見積", "見積", "見積金額", "quotation", "estimate"],
    "estimate": ["見積書", "御見積", "見積", "estimate"],
    "purchase_order": ["発注書", "注文書", "ご注文", "purchase order", "purchaseorder"],
    "order": ["注文書", "発注書", "ご注文", "order"],
    "delivery_note": ["納品書", "納品", "delivery note", "deliverynote"],
    "receipt": ["領収書", "領収", "レシート", "receipt"],
    "statement": ["取引明細", "明細書", "statement"],
}

# 語彙として弱すぎる（どの帳票にも出る）語は無視する。
_STOPWORDS = {"合計", "金額", "日付", "no", "total", "amount", "date"}

_FILENAME_WEIGHT = 3.0  # ファイル名一致は本文一致より重い（命名は種別を強く示す）
_TEXT_WEIGHT = 1.0


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


def synonyms_for(doc_type: str) -> list[str]:
    """doc_type の代表語彙（doc_type 名そのもの＋既知の別名）。"""
    dt = (doc_type or "").strip()
    words = [dt] if dt else []
    words += DOC_TYPE_SYNONYMS.get(dt.lower(), [])
    return words


def build_candidate(doc_type: str, field_labels: list[str] | None = None) -> ClassifyCandidate:
    """スキーマ 1 件から候補を作る。語彙＝doc_type 別名＋項目ラベル（ストップワード除く）。"""
    kws = list(synonyms_for(doc_type))
    for lbl in field_labels or []:
        lbl = (lbl or "").strip()
        if lbl and lbl.lower() not in _STOPWORDS and len(lbl) >= 2:
            kws.append(lbl)
    # 重複排除（順序維持）
    seen: set[str] = set()
    uniq: list[str] = []
    for k in kws:
        kl = k.lower()
        if kl and kl not in seen:
            seen.add(kl)
            uniq.append(k)
    return ClassifyCandidate(doc_type=doc_type, keywords=uniq)


def _norm(s: str) -> str:
    # 全角スペース・改行を潰し、英字は小文字化（日本語はそのまま）。
    return re.sub(r"\s+", " ", (s or "")).lower()


def _hit(keyword: str, haystack: str) -> bool:
    kw = keyword.lower().strip()
    return bool(kw) and kw in haystack


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
        score = 0.0
        hits: list[str] = []
        # 語彙ごとに「一致したか」を見る（同語の連呼で偏らないよう存在ベース）
        for kw in cand.keywords:
            if _hit(kw, fn):
                score += _FILENAME_WEIGHT
                hits.append(kw)
            elif _hit(kw, tx):
                score += _TEXT_WEIGHT
                hits.append(kw)
        scores[cand.doc_type] = score
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
