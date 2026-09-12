"""A/B 実測の統計部分（McNemar）。

前回（2026-09-06）の Phase 4 実測は、全体正解率 0.864 対 0.780 を目視で比べただけで、
「この差がノイズの範囲か」を判断する根拠が無かった。率の引き算は試行数を見ないので、
2 件の文書・5 試行でも同じ見た目の差が出る。対応のある検定を挟んで、出荷判断が
数字で止められるようにする。
"""

from __future__ import annotations

from newfan_golden.region_ab import (
    _norm,
    field_type_for,
    hint_summary,
    mcnemar,
    per_doc_net,
    score_key,
)


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


class Test住所の採点:
    """住所は抽出値と正解の**両方**を norm_address_jp に通してから比べる（ADR-0007）。

    第 3 回計測で住所は 対照のみ 18 / 介入のみ 5（p = 0.011）だったが、外れ方の大半は
    「郵便番号や『本社』を値に含めるか」の慣例の違い。プロダクトの正規化と正解の慣例を
    同じ土俵に乗せる。建物名を落とした値は慣例ではなく取りこぼしなので、不正解のまま残す。
    """

    def test_住所の項目だけ_address_jp(self) -> None:
        assert field_type_for("issuer_address") == "address_jp"
        assert field_type_for("customer_address") == "address_jp"
        assert field_type_for("issuer_name") == "string"
        assert field_type_for("total_amount") == "string"  # 金額は従来どおり型を付けない

    def test_郵便番号付きの抽出値は全角の正解と一致する(self) -> None:
        gold = "東京都品川区北品川５－１０－２０サンプルビル２Ｆ"
        got = "〒141-0001 東京都品川区北品川5-10-20 サンプルビル2F"
        assert score_key("issuer_address", got) == score_key("issuer_address", gold)

    def test_第3回で観測した外れ値が正解になる(self) -> None:
        gold = "仙台市泉区紫山3-1-4"
        assert score_key("issuer_address", "981-3205 仙台市泉区紫山3-1-4") == score_key("issuer_address", gold)
        assert score_key("issuer_address", "本社 981-3205 仙台市泉区紫山3-1-4") == score_key("issuer_address", gold)

    def test_建物名を落とした値は不正解のまま(self) -> None:
        gold = "東京都品川区北品川５－１０－２０サンプルビル２Ｆ"
        assert score_key("issuer_address", "東京都品川区北品川5-10-20") != score_key("issuer_address", gold)

    def test_正解側にも通す(self) -> None:
        """正解データに郵便番号が残っていても（修正漏れ）、計測は歪まない。"""
        assert score_key("customer_address", "〒596-0006 大阪府岸和田市春木若松町1026-56") == score_key(
            "customer_address", "大阪府岸和田市春木若松町1026-56"
        )

    def test_住所以外の項目には住所の正規化を掛けない(self) -> None:
        # 社名の先頭の「本社」や「支店」は値の一部。住所の規則で剥がしてはいけない
        assert score_key("issuer_name", "本社 わくわく物産株式会社") == _norm("本社 わくわく物産株式会社")
        assert score_key("issuer_name", "本社 わくわく物産株式会社") != score_key("issuer_name", "わくわく物産株式会社")

    def test_未検出は空文字(self) -> None:
        assert score_key("issuer_address", None) == ""
        assert score_key("issuer_address", "〒") == ""  # 剥がして空 → None → 空文字


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


class Test再開:
    """--resume: 前回の出力にある (帳票, 試行, アーム) は回さず、欠けた対だけ回す。"""

    def test_前回の行を再利用し欠けた試行だけ抽出する(self, monkeypatch, tmp_path) -> None:
        import newfan_golden.region_ab as ab
        from newfan_golden.dataset import GoldField, GoldenDoc

        img = tmp_path / "a.png"
        img.write_bytes(b"x")
        doc = GoldenDoc(document_id="d1", doc_type="t1", image_uri=str(img),
                        fields=[GoldField(name="a", value="X"), GoldField(name="b", value="")])
        calls: list[tuple[int, str]] = []
        monkeypatch.setattr(ab, "_put_schema", lambda c, body: {"id": body["doc_type"]})
        monkeypatch.setattr(ab, "_upload", lambda c, p: "docid")
        monkeypatch.setattr(ab, "_extract", lambda c, d, s, t: "succeeded")
        monkeypatch.setattr(ab, "_result", lambda c, d: {"run_id": "r", "fields": [{"name": "a", "value_raw": "X"}],
                                                          "region_stats": {"hints": {"given": ["a"]}}})

        class _Client:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def delete(self, *a, **k): calls.append((-1, "delete"))

        monkeypatch.setattr(ab.httpx, "Client", _Client)
        prev = {
            "control_runs": [{"document_id": "d1", "trial": 0, "hits": 0, "total": 1, "run_id": None,
                              "field_hits": {"a": False}, "hints": None}],
            "treat_runs": [{"document_id": "d1", "trial": 0, "hits": 1, "total": 1, "run_id": None,
                            "field_hits": {"a": True}, "hints": {"given": ["a"], "outcomes": {"a": "followed"}}},
                           {"document_id": "d1", "trial": 1, "hits": 1, "total": 1, "run_id": None,
                            "field_hits": {"a": True}, "hints": {"given": ["a"]}}],
        }
        rep = ab.run([doc], {"t1": {}, "_positional": {}}, "http://x", "tok", 2, 1.0, resume=prev)
        # 欠けていたのは control の trial 1 だけ → 抽出（delete）は 1 回
        assert calls.count((-1, "delete")) == 1
        assert [r["trial"] for r in rep["control_runs"]] == [0, 1]
        assert [r["trial"] for r in rep["treat_runs"]] == [0, 1]
        # 再利用した行の hints はそのまま残る
        assert rep["treat_runs"][0]["hints"]["outcomes"] == {"a": "followed"}
        # 集計は 4 行から作り直される: control 1/2、treat 2/2。正解値の無い b は表に出ない
        names = {f["name"] for f in rep["fields"]}
        assert names == {"d1::a"}
        f = rep["fields"][0]
        assert f["control"] == "1/2" and f["treat"] == "2/2"
        assert rep["per_doc_net"]["d1"] == {"pairs": 2, "control_hits": 1, "treat_hits": 2, "net": 1}

    def test_field_hits_の無い古い出力は再利用しない(self) -> None:
        from newfan_golden.region_ab import _done_rows
        assert _done_rows({"control_runs": [{"document_id": "d", "trial": 0, "hits": 1}]}) == {}
        assert _done_rows(None) == {}


class Testスキーマの型:
    """A/B の 2 版は住所だけ address_jp、他は string（ADR-0007）。対照と介入で同じ型。"""

    def test_住所は両版とも_address_jp_で保存され採点は正規化後で一致する(
        self, monkeypatch, tmp_path
    ) -> None:
        import newfan_golden.region_ab as ab
        from newfan_golden.dataset import GoldField, GoldenDoc

        img = tmp_path / "a.png"
        img.write_bytes(b"x")
        doc = GoldenDoc(
            document_id="d1", doc_type="t1", image_uri=str(img),
            fields=[GoldField(name="issuer_address", value="東京都品川区北品川５－１０－２０サンプルビル２Ｆ"),
                    GoldField(name="issuer_name", value="サンプル株式会社")],
        )
        bodies: list[dict] = []

        def _put(c, body):
            bodies.append(body)
            return {"id": body["doc_type"]}

        monkeypatch.setattr(ab, "_put_schema", _put)
        monkeypatch.setattr(ab, "_upload", lambda c, p: "docid")
        monkeypatch.setattr(ab, "_extract", lambda c, d, s, t: "succeeded")
        # プロダクトは address_jp の value_normalized を返す（郵便番号なし・空白なし）。
        # 対照は郵便番号付きの value_raw しか無い run を模す
        monkeypatch.setattr(ab, "_result", lambda c, d: {
            "run_id": "r",
            "fields": [{"name": "issuer_address",
                        "value_raw": "〒141-0001 東京都品川区北品川5-10-20 サンプルビル2F"},
                       {"name": "issuer_name", "value_raw": "サンプル株式会社"}],
            "region_stats": {},
        })

        class _Client:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def delete(self, *a, **k): pass

        monkeypatch.setattr(ab.httpx, "Client", _Client)
        rep = ab.run([doc], {"t1": {}, "_positional": {}}, "http://x", "tok", 1, 1.0)

        assert [b["doc_type"] for b in bodies] == ["ab_control_t1", "ab_treat_t1"]
        for b in bodies:
            types = {f["name"]: f["type"] for f in b["fields"]}
            assert types == {"issuer_address": "address_jp", "issuer_name": "string"}
        by_name = {f["name"]: f for f in rep["fields"]}
        assert by_name["d1::issuer_address"]["control"] == "1/1"
        assert by_name["d1::issuer_address"]["treat"] == "1/1"
