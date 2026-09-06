"""A/B 実測の統計部分（McNemar）。

前回（2026-09-06）の Phase 4 実測は、全体正解率 0.864 対 0.780 を目視で比べただけで、
「この差がノイズの範囲か」を判断する根拠が無かった。率の引き算は試行数を見ないので、
2 件の文書・5 試行でも同じ見た目の差が出る。対応のある検定を挟んで、出荷判断が
数字で止められるようにする。
"""

from __future__ import annotations

from newfan_golden.region_ab import _norm, mcnemar


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
