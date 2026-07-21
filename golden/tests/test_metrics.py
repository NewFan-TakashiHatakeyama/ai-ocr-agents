"""§14.2 の指標定義を固定するテスト。

指標の意味がずれるとゲートが意味を失う（例: 有害率の分母を全項目にすると、
補正が壊していても数字が小さく出て通ってしまう）。定義そのものを検査する。
"""

from __future__ import annotations

from newfan_golden.metrics import (
    HARMFUL_RATE_LIMIT,
    GoldField,
    PredField,
    Report,
    check_gate,
    evaluate,
    score_document,
)


def _g(name: str, value: str | None, critical: bool = False) -> GoldField:
    return GoldField(name=name, value=value, critical=critical)


def test_exact_match_は正規化後の一致で数える() -> None:
    s = score_document(
        "d1",
        [_g("合計金額", "7003"), _g("発行日", "2026-07-16")],
        [PredField("合計金額", " 7003 "), PredField("発行日", "2026-07-15")],
    )
    assert (s.matched, s.total) == (1, 2)


def test_未予測の項目は_precision_の分母に入らない() -> None:
    # 「拾えなかった」と「間違えた」は別物。空値まで分母に入れると Precision が
    # Recall と同じ数字になり、取りこぼしと誤りを区別できなくなる。
    s = score_document(
        "d1",
        [_g("合計金額", "7003"), _g("備考", "至急")],
        [PredField("合計金額", "7003"), PredField("備考", None)],
    )
    assert (s.predicted, s.matched, s.total) == (1, 1, 2)

    r = Report(docs=[s])
    assert r.precision == 1.0
    assert r.recall == 0.5


def test_critical_は別集計される() -> None:
    r = Report(
        docs=[
            score_document(
                "d1",
                [_g("合計金額", "7003", critical=True), _g("備考", "至急")],
                [PredField("合計金額", "7000"), PredField("備考", "至急")],
            )
        ]
    )
    assert r.exact_match == 0.5
    assert r.critical_exact_match == 0.0  # critical だけ見ると全滅


def test_人手確認が要る文書は_stp_に数えない() -> None:
    stp = score_document("d1", [_g("a", "1")], [PredField("a", "1", review_status="auto")])
    not_stp = score_document(
        "d2", [_g("a", "1")], [PredField("a", "1", review_status="pending")]
    )
    assert stp.stp is True
    assert not_stp.stp is False
    assert Report(docs=[stp, not_stp]).stp_rate == 0.5


def test_有害率は_補正が正解を壊した件数だけを数える() -> None:
    # 補正前 7003（正解）→ 補正後 7000（誤り）= 有害
    harmful = score_document(
        "d1", [_g("金額", "7003")], [PredField("金額", "7000", corrected_from="7003")]
    )
    # 補正前 7OO3（誤り）→ 補正後 7003（正解）= 有害ではない（むしろ直っている）
    helpful = score_document(
        "d2", [_g("金額", "7003")], [PredField("金額", "7003", corrected_from="7OO3")]
    )
    assert (harmful.harmful, harmful.corrected) == (1, 1)
    assert (helpful.harmful, helpful.corrected) == (0, 1)
    assert Report(docs=[harmful, helpful]).harmful_rate == 0.5


def test_補正が働かなければ有害率は0() -> None:
    r = Report(docs=[score_document("d1", [_g("金額", "7003")], [PredField("金額", "7000")])])
    assert r.harmful_rate == 0.0  # ゼロ除算せず「壊していない」


def test_ゲートは有害率の上限超過でブロックする() -> None:
    # 1/1 = 100% > 0.1%
    r = Report(
        docs=[score_document("d1", [_g("金額", "7003")], [PredField("金額", "7000", corrected_from="7003")])]
    )
    gate = check_gate(r, baseline=None)
    assert gate.passed is False
    assert "有害率" in gate.reasons[0]
    assert HARMFUL_RATE_LIMIT == 0.001


def test_ゲートは加重平均の劣化でブロックする() -> None:
    gold = [_g("a", "1", critical=True)] + [_g(f"f{i}", "1") for i in range(199)]
    base_pred = [PredField("a", "1")] + [PredField(f"f{i}", "1") for i in range(199)]
    # 200 項目中 1 項目だけ落とす = exact_match -0.5pt。critical は維持されるので
    # 加重平均では -0.25pt にとどまり、上限 0.5pt 以内で通る。
    cur_pred = [PredField("a", "1")] + [
        PredField(f"f{i}", "x" if i == 0 else "1") for i in range(199)
    ]
    baseline = evaluate([("d1", gold, base_pred)])
    current = evaluate([("d1", gold, cur_pred)])
    assert check_gate(current, baseline).passed is True

    # critical を壊すと加重平均が一気に落ちてブロックされる
    broken = evaluate([("d1", gold, [PredField("a", "x")] + base_pred[1:])])
    gate = check_gate(broken, baseline)
    assert gate.passed is False
    assert any("加重平均" in r for r in gate.reasons)


def test_ゲートは改善ならブロックしない() -> None:
    gold = [_g("a", "1", critical=True), _g("b", "2")]
    baseline = evaluate([("d1", gold, [PredField("a", "1"), PredField("b", "x")])])
    current = evaluate([("d1", gold, [PredField("a", "1"), PredField("b", "2")])])
    assert check_gate(current, baseline).passed is True


def test_有害率の悪化はベースライン比でもブロックする() -> None:
    # 上限未満でも「前回より悪化」ならブロックする（§14.2）
    gold = [_g(f"f{i}", "1") for i in range(2000)]
    clean = [PredField(f"f{i}", "1") for i in range(2000)]
    worse = [PredField("f0", "x", corrected_from="1")] + clean[1:]
    baseline = evaluate([("d1", gold, clean)])
    current = evaluate([("d1", gold, worse)])
    assert baseline.harmful_rate == 0.0
    gate = check_gate(current, baseline)
    assert gate.passed is False
