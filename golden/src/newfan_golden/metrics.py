"""ゴールデンセットの精度指標（§14.2 / DD-03）。

指標は設計書の定義に従う:
- field-level Exact Match（**正規化後**の表現で比較）
- Precision / Recall（critical 別も出す）
- STP シミュレーション率（人手修正なしで確定できた割合）
- 補正の有害率（正しい値を壊した率。**0.1% 未満がリリースゲート**）

リリースゲート（§14.2）:
- 加重平均で -0.5pt 超の劣化でブロック
- 有害率の悪化でブロック
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

# §14.2 のリリースゲート
HARMFUL_RATE_LIMIT = 0.001  # 0.1%
REGRESSION_LIMIT_PT = 0.5  # 加重平均 -0.5pt 超で劣化とみなす


@dataclass(frozen=True)
class GoldField:
    """正解ラベル 1 項目。value は正規化後の表現（§14.2）。"""

    name: str
    value: Optional[str]
    critical: bool = False


@dataclass(frozen=True)
class PredField:
    """抽出結果 1 項目。

    value は正規化後（value_normalized 相当）。corrected_from には LLM 補正やルールが
    値を書き換えた場合の「元の値」を入れる。有害率（正しい値を壊した率）の判定に使う。
    """

    name: str
    value: Optional[str]
    review_status: str = "auto"  # auto / approved / pending / corrected
    corrected_from: Optional[str] = None


@dataclass
class DocScore:
    document_id: str
    matched: int = 0
    total: int = 0
    critical_matched: int = 0
    critical_total: int = 0
    predicted: int = 0  # 予測した（値がある）項目数 = Precision の分母
    harmful: int = 0  # 補正が正解を壊した件数
    corrected: int = 0  # 補正が働いた件数 = 有害率の分母
    stp: bool = False  # 人手確認なしで確定できたか


@dataclass
class Report:
    docs: list[DocScore] = field(default_factory=list)

    def _sum(self, attr: str) -> int:
        return sum(getattr(d, attr) for d in self.docs)

    @property
    def exact_match(self) -> float:
        """field-level Exact Match（正規化後）。全ドキュメントの項目をまとめて数える。"""
        total = self._sum("total")
        return self._sum("matched") / total if total else 0.0

    @property
    def critical_exact_match(self) -> float:
        total = self._sum("critical_total")
        return self._sum("critical_matched") / total if total else 0.0

    @property
    def precision(self) -> float:
        """予測した項目のうち正しかった割合。"""
        predicted = self._sum("predicted")
        return self._sum("matched") / predicted if predicted else 0.0

    @property
    def recall(self) -> float:
        """正解項目のうち拾えた割合。"""
        total = self._sum("total")
        return self._sum("matched") / total if total else 0.0

    @property
    def stp_rate(self) -> float:
        return sum(1 for d in self.docs if d.stp) / len(self.docs) if self.docs else 0.0

    @property
    def harmful_rate(self) -> float:
        """補正が正しい値を壊した率。分母は「補正が働いた件数」（§14.2）。

        補正が一度も働かなければ 0.0（壊しようがない）。
        """
        corrected = self._sum("corrected")
        return self._sum("harmful") / corrected if corrected else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "documents": len(self.docs),
            "fields": self._sum("total"),
            "exact_match": round(self.exact_match, 4),
            "critical_exact_match": round(self.critical_exact_match, 4),
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "stp_rate": round(self.stp_rate, 4),
            "harmful_rate": round(self.harmful_rate, 4),
        }


def _norm(v: Optional[str]) -> str:
    """比較用の正規化。値の正規化自体は抽出側（§5.6）の責務なので、ここでは
    前後空白と None の揺れだけを吸収する（ここで独自に正規化すると、抽出側の
    正規化バグを回帰が見逃す）。"""
    return (v or "").strip()


def score_document(
    document_id: str, gold: Iterable[GoldField], pred: Iterable[PredField]
) -> DocScore:
    pred_by_name = {p.name: p for p in pred}
    s = DocScore(document_id=document_id)
    needs_human = False

    for g in gold:
        s.total += 1
        if g.critical:
            s.critical_total += 1
        p = pred_by_name.get(g.name)
        gv = _norm(g.value)
        pv = _norm(p.value) if p else ""

        if pv:
            s.predicted += 1
        if p and p.review_status in ("pending", "corrected"):
            # pending=人手確認待ち, corrected=人手が直した → STP ではない
            needs_human = True

        ok = pv == gv
        if ok:
            s.matched += 1
            if g.critical:
                s.critical_matched += 1

        # 有害率: 補正が働いた項目のうち、「補正前は正解だったのに補正後に外れた」もの
        if p is not None and p.corrected_from is not None:
            s.corrected += 1
            if _norm(p.corrected_from) == gv and not ok:
                s.harmful += 1

    s.stp = not needs_human
    return s


def evaluate(pairs: Iterable[tuple[str, list[GoldField], list[PredField]]]) -> Report:
    return Report(docs=[score_document(d, g, p) for d, g, p in pairs])


@dataclass(frozen=True)
class GateResult:
    passed: bool
    reasons: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "reasons": self.reasons}


def check_gate(current: Report, baseline: Optional[Report] = None) -> GateResult:
    """§14.2 のリリースゲート。

    baseline が無い（初回）ときは有害率だけを見る。劣化は比較対象が無いと判定できない。
    """
    reasons: list[str] = []

    if current.harmful_rate >= HARMFUL_RATE_LIMIT:
        reasons.append(
            f"有害率 {current.harmful_rate:.4f} が上限 {HARMFUL_RATE_LIMIT} 以上"
            "（補正が正しい値を壊している）"
        )

    if baseline is not None:
        # 加重平均: critical を重く見る（critical を壊す方が実害が大きい）
        def weighted(r: Report) -> float:
            return 0.5 * r.exact_match + 0.5 * r.critical_exact_match

        drop_pt = (weighted(baseline) - weighted(current)) * 100
        if drop_pt > REGRESSION_LIMIT_PT:
            reasons.append(
                f"加重平均が {drop_pt:.2f}pt 劣化（上限 {REGRESSION_LIMIT_PT}pt）"
            )
        if current.harmful_rate > baseline.harmful_rate:
            reasons.append(
                f"有害率が悪化 {baseline.harmful_rate:.4f} → {current.harmful_rate:.4f}"
            )

    return GateResult(passed=not reasons, reasons=reasons)
