"""A/B 実測の統計部分（McNemar）。

前回（2026-09-06）の Phase 4 実測は、全体正解率 0.864 対 0.780 を目視で比べただけで、
「この差がノイズの範囲か」を判断する根拠が無かった。率の引き算は試行数を見ないので、
2 件の文書・5 試行でも同じ見た目の差が出る。対応のある検定を挟んで、出荷判断が
数字で止められるようにする。
"""

from __future__ import annotations

from newfan_golden.region_ab import _norm, hint_summary, mcnemar, per_doc_net


def _pairs(rows: list[tuple[bool, bool]]) -> dict[tuple[str, str, int], dict[str, bool]]:
    return {("d", "f", i): {"control": c, "treat": t} for i, (c, t) in enumerate(rows)}


def test_介入が一貫して勝つと有意になる() -> None:
    got = mcnemar(_pairs([(False, True)] * 12))
    assert got["treat_only"] == 12
    assert got["control_only"] == 0
    assert got["p_value"] is not None and got["p_value"] < 0.05
    assert got["verdict"] == "介入が有意に良い"


def test_勝ち負けが五分なら有意差なし() -> None:
    got = mcnemar(_pairs([(True, False), (False, True)] * 6))
    assert got["discordant"] == 12
    assert got["p_value"] == 1.0
    assert "有意差なし" in got["verdict"]


def test_不一致対が少なければ差を主張できない() -> None:
    """**率の差が大きく見えても**、対が少なければ何も言えないことを固定する。"""
    got = mcnemar(_pairs([(False, True), (False, True)]))
    assert got["discordant"] == 2
    assert got["p_value"] == 0.5  # 2 対では 5% を切れない
    assert "有意差なし" in got["verdict"]


def test_一致対は差の情報を持たない() -> None:
    got = mcnemar(_pairs([(True, True)] * 50 + [(False, False)] * 50))
    assert got["discordant"] == 0
    assert got["p_value"] is None
    assert "材料が無い" in got["verdict"]


def test_片方の抽出が失敗した対は捨てる() -> None:
    """アームの片方が失敗した試行を数えると、成功側の勝ちに化ける。"""
    paired: dict[tuple[str, str, int], dict[str, bool]] = {
        ("d", "f", 0): {"treat": True},  # control が失敗
        ("d", "f", 1): {"control": True},  # treat が失敗
        ("d", "f", 2): {"control": False, "treat": True},
    }
    got = mcnemar(paired)
    assert got["discordant"] == 1
    assert got["treat_only"] == 1


def test_位置依存の項目だけを取り出せる() -> None:
    paired: dict[tuple[str, str, int], dict[str, bool]] = {
        ("d", "issuer_name", 0): {"control": False, "treat": True},
        ("d", "invoice_no", 0): {"control": True, "treat": False},
    }
    got = mcnemar(paired, only={"issuer_name"})
    assert got["discordant"] == 1
    assert got["treat_only"] == 1
    assert got["control_only"] == 0


def test_介入が一貫して負けると悪化と判定する() -> None:
    got = mcnemar(_pairs([(True, False)] * 12))
    assert got["verdict"] == "介入が有意に悪い"


class Test比較の正規化:
    """表記の揺れを同一視する（測りたいのは実体の取り違えであって字面ではない）。

    実測すると、抽出値と正解の差の大半が敬称・全角半角・区切り記号だった。
    別物として数えると両アームとも同じだけ外れ、差が見えなくなる（1/7 まで落ちた）。
    """

    def test_敬称は無視する(self) -> None:
        assert _norm("大熊 和一") == _norm("大熊和一様")
        assert _norm("株式会社山田製作所") == _norm("株式会社山田製作所 御中")

    def test_全角半角を同一視する(self) -> None:
        assert _norm("美しが丘１８丁目５番地２号") == _norm("美しが丘 18丁目5番地2号")

    def test_通貨記号と桁区切りを落とす(self) -> None:
        assert _norm("395217") == _norm("￥395,217")
        assert _norm("58300") == _norm("58,300円")

    def test_値そのものの違いは残る(self) -> None:
        """正規化は**取り違えや欠落を隠さない**。ここが緩むと計測が無意味になる。"""
        assert _norm("【サンプル】ビズリフォーム株式会社") != _norm("【サンプル】ビズリフォー")
        assert _norm("わくわく物産株式会社") != _norm("株式会社エイビーエム")
        assert _norm("395217") != _norm("359289")

    def test_未検出は空文字(self) -> None:
        assert _norm(None) == ""


def _run(doc: str, trial: int, field_hits: dict[str, bool], hints: dict | None = None) -> dict:
    return {
        "document_id": doc,
        "trial": trial,
        "hits": sum(field_hits.values()),
        "total": len(field_hits),
        "run_id": None,
        "field_hits": field_hits,
        "hints": hints,
    }


class Test帳票ごとの純増減:
    def test_両アームが成功した試行だけを足す(self) -> None:
        control = [_run("d1", 0, {"a": True, "b": True}), _run("d1", 1, {"a": True, "b": False})]
        treat = [
            _run("d1", 0, {"a": True, "b": False}),
            _run("d1", 1, {"a": True, "b": True}),
            _run("d1", 2, {"a": True, "b": True}),  # 対照が失敗した試行 → 数えない
        ]
        got = per_doc_net(control, treat)
        assert got == {"d1": {"pairs": 2, "control_hits": 3, "treat_hits": 3, "net": 0}}

    def test_純減が出る(self) -> None:
        control = [_run("d1", i, {"a": True, "b": True}) for i in range(5)]
        treat = [_run("d1", i, {"a": True, "b": i == 0}) for i in range(5)]
        assert per_doc_net(control, treat)["d1"]["net"] == -4


class Testヒントの内訳:
    def test_outcomes_と_dropped_を項目ごとに数える(self) -> None:
        treat = [
            _run("d1", 0, {"a": True, "b": True},
                 {"given": ["a"], "dropped": {"b": "kind_conflict"},
                  "outcomes": {"a": "followed"}}),
            _run("d1", 1, {"a": True, "b": True},
                 {"given": ["a"], "dropped": {"b": "type_mismatch"},
                  "outcomes": {"a": "rejected"}}),
        ]
        got = hint_summary([], treat)
        assert got["per_field"]["d1::a"]["given"] == 2
        assert got["per_field"]["d1::a"]["followed"] == 1
        assert got["per_field"]["d1::a"]["rejected"] == 1
        assert got["per_field"]["d1::b"]["dropped"] == {"kind_conflict": 1, "type_mismatch": 1}

    def test_落とした_捨てた項目を対照と対にする(self) -> None:
        """G4: 落としたことで悪くなっていないかは、同じ試行の対照との差で見る。"""
        control = [_run("d1", 0, {"a": True, "b": False, "c": True})]
        treat = [
            _run("d1", 0, {"a": True, "b": True, "c": False},
                 {"given": ["a", "c"], "dropped": {"b": "no_spans_in_region"},
                  "outcomes": {"a": "followed", "c": "rejected"}}),
        ]
        got = hint_summary(control, treat)
        # b（dropped）: 対照 ×→介入 ○、c（rejected）: 対照 ○→介入 ×。a は followed なので入らない
        assert got["affected"] == {"pairs": 2, "control_hits": 1, "treat_hits": 1, "delta": 0}
        assert {r["field"] for r in got["affected_rows"]} == {"b", "c"}
        assert next(r for r in got["affected_rows"] if r["field"] == "b")["why"] == "no_spans_in_region"

    def test_no_evidence_は_affected_に入れない(self) -> None:
        control = [_run("d1", 0, {"a": True})]
        treat = [_run("d1", 0, {"a": False},
                      {"given": ["a"], "dropped": {}, "outcomes": {"a": "no_evidence"}})]
        got = hint_summary(control, treat)
        assert got["affected"]["pairs"] == 0
        assert got["per_field"]["d1::a"]["no_evidence"] == 1

    def test_対照が失敗した試行は対にしない(self) -> None:
        treat = [_run("d1", 3, {"a": False},
                      {"given": ["a"], "dropped": {}, "outcomes": {"a": "rejected"}})]
        got = hint_summary([], treat)
        assert got["affected"]["pairs"] == 0
        assert got["per_field"]["d1::a"]["rejected"] == 1

    def test_hints_の無い行は素通り(self) -> None:
        got = hint_summary([_run("d1", 0, {"a": True})], [_run("d1", 0, {"a": True})])
        assert got == {"per_field": {}, "affected": {"pairs": 0, "control_hits": 0,
                                                     "treat_hits": 0, "delta": 0},
                       "affected_rows": []}
