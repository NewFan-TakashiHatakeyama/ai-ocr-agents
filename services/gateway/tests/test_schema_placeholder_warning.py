"""記入例語の例示値の警告（設計 region-field-add-and-hint-v2 §2.5 / region-template-editor §6）。

テンプレート化／領域編集画面は、例示値が記入例語（「〇〇株式会社」「YYYY/MM/DD」「住所1」）
でも保存を止めない。実行時は KIE の前の事前ガードが ``placeholder_example`` でその項目の
ヒントを落とすだけなので、黙っていると作者は「記入例の帳票で領域を引いた」ことに
気付けない。gateway は orchestrator と**同じ判定**（``newfan_schemas.is_placeholder_example``）
で、判定 API（画面の行の注記）と保存応答の ``warnings``（保存後の通知）を返す。
"""

from __future__ import annotations

import json
import pathlib
from types import SimpleNamespace

from gw_helpers import auth

from newfan_gateway.dto import EXAMPLE_VALUE_CHECK_MAX

CHECK = "/v1/schemas/example-values/check"
RECT = [0.30, 0.02, 0.72, 0.09]


def _region(example: str | None, page: int = 1) -> dict:
    return {"page": page, "rect": RECT, "example_value": example, "origin": "ghost"}


def _check(ctx: SimpleNamespace, values: list, role: str = "admin") -> SimpleNamespace:
    r = ctx.client.post(CHECK, headers=auth(role), json={"values": values})
    return SimpleNamespace(status=r.status_code, json=r.json())


def _put(ctx: SimpleNamespace, fields: list[dict], doc_type: str = "invoice") -> SimpleNamespace:
    r = ctx.client.put(
        "/v1/schemas", headers=auth("admin"), json={"doc_type": doc_type, "fields": fields}
    )
    return SimpleNamespace(status=r.status_code, json=r.json())


# ---------- 判定 API ----------


def test_check_は値ごとに記入例語かを同じ順で返す(ctx: SimpleNamespace) -> None:
    values = [
        "自社名(ロゴや社判も登録できます)",  # sample13（第 4 回計測 S3 で害を出した実例）
        "株式会社千曲川ホーム",
        "YYYY/MM/DD",
        "〇〇株式会社",
        "東京都千代田区住所1住所2ビル名等",
        "会社名 株式会社山田",  # 見出し語＋値は値
        "395,217",
        "",
        None,
    ]
    res = _check(ctx, values)
    assert res.status == 200
    assert [i["value"] for i in res.json["items"]] == values  # 受け取った値をそのまま返す
    assert [i["placeholder"] for i in res.json["items"]] == [
        True, False, True, True, True, False, False, False, False,
    ]


def test_check_は保存される形_消毒後_で判定する(ctx: SimpleNamespace) -> None:
    """orchestrator は sanitize_example_value の後の値で判定する。改行は**削除**されるので
    「Lorem\\nipsum」は「Loremipsum」として当たる。制御文字だけの値は消毒で空（例示値なし）。"""
    res = _check(ctx, ["Lorem\nipsum", "\x00\n", "  〇〇 様  "])
    assert [i["placeholder"] for i in res.json["items"]] == [True, False, True]
    # value は受け取ったまま（画面が自分の値と突き合わせる）
    assert res.json["items"][0]["value"] == "Lorem\nipsum"


def test_check_は空の一覧も受ける(ctx: SimpleNamespace) -> None:
    res = _check(ctx, [])
    assert res.status == 200
    assert res.json == {"items": []}


def test_check_は保存と同じく_admin_だけ(ctx: SimpleNamespace) -> None:
    for role in ("reviewer", "uploader", "viewer"):
        assert _check(ctx, ["〇〇株式会社"], role=role).status == 403
    # 認証なしも E5001（§6.5 で 403 に対応づけている）
    r = ctx.client.post(CHECK, json={"values": ["〇〇株式会社"]})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "E5001"


def test_check_は件数の上限を超えると_422(ctx: SimpleNamespace) -> None:
    assert _check(ctx, ["a"] * EXAMPLE_VALUE_CHECK_MAX).status == 200
    assert _check(ctx, ["a"] * (EXAMPLE_VALUE_CHECK_MAX + 1)).status == 422


def test_check_は第4回の例示値94件で_sample13_由来の3件だけが記入例語(ctx: SimpleNamespace) -> None:
    """API を通しても判定が 1 件も変わらないこと（packages/schemas の回帰テストと同じ期待値）。"""
    path = (
        pathlib.Path(__file__).resolve().parents[3] / "golden" / "data" / "region_ab_example_values.json"
    )
    items = json.loads(path.read_text(encoding="utf-8"))["items"]
    assert len(items) >= 90
    res = _check(ctx, [i["example_value"] for i in items])
    assert res.status == 200
    flagged = sorted(
        (i["doc_type"], i["field"])
        for i, r in zip(items, res.json["items"], strict=True)
        if r["placeholder"]
    )
    assert flagged == [
        ("s3_sample2_from_sample13", "customer_name"),
        ("s3_sample2_from_sample13", "issuer_address"),
        ("s3_sample2_from_sample13", "issuer_name"),
    ]


# ---------- 保存応答の warnings ----------


def test_put_は記入例語の例示値を項目名と値で警告し_保存は止めない(ctx: SimpleNamespace) -> None:
    res = _put(
        ctx,
        [
            {"name": "issuer_name", "type": "string", "region": _region("自社名(ロゴや社判も登録できます)")},
            {"name": "total", "type": "money_jpy", "region": _region("395,217")},
            {"name": "issue_date", "type": "date", "region": _region("YYYY/MM/DD")},
            {"name": "note", "type": "string"},  # 領域なし
            {"name": "customer", "type": "string", "region": _region(None)},  # 例示値なし
        ],
    )
    assert res.status == 200
    # 並びは項目の順。code は将来の警告の種類を区別するため
    assert res.json["warnings"] == [
        {
            "code": "placeholder_example",
            "field": "issuer_name",
            "example_value": "自社名(ロゴや社判も登録できます)",
        },
        {"code": "placeholder_example", "field": "issue_date", "example_value": "YYYY/MM/DD"},
    ]
    # 保存は通っていて、例示値もそのまま残る（消すかどうかは作者が決める）
    got = ctx.client.get("/v1/schemas/invoice", headers=auth("admin")).json()
    assert got["version"] == res.json["version"]
    by_name = {f["name"]: f for f in got["fields"]}
    assert by_name["issuer_name"]["region"]["example_value"] == "自社名(ロゴや社判も登録できます)"


def test_put_の応答は既存のキーを変えず_warnings_を足すだけ(ctx: SimpleNamespace) -> None:
    """旧編集画面・chat 経路は warnings を読まない。既存のキーと値は GET と同じまま。"""
    res = _put(ctx, [{"name": "total", "type": "money_jpy", "region": _region("395,217")}])
    assert res.status == 200
    assert res.json["warnings"] == []
    got = ctx.client.get("/v1/schemas/invoice", headers=auth("admin")).json()
    assert "warnings" not in got  # GET の応答形は変えない
    assert {k: v for k, v in res.json.items() if k != "warnings"} == got


def test_put_の警告は消毒後の値で出す(ctx: SimpleNamespace) -> None:
    res = _put(ctx, [{"name": "customer", "type": "string", "region": _region("Lorem\nipsum")}])
    assert res.status == 200
    assert res.json["warnings"] == [
        {"code": "placeholder_example", "field": "customer", "example_value": "Loremipsum"}
    ]
    assert res.json["fields"][0]["region"]["example_value"] == "Loremipsum"


def test_put_の編集往復でも既存の記入例語は警告し_例示値を消せば消える(ctx: SimpleNamespace) -> None:
    """触っていない既存領域の例示値も「この版でヒントに使われない」ので毎回伝える。
    画面の「例示値を消す」（example_value: null）で外した版では出ない。"""
    first = _put(ctx, [{"name": "issuer_name", "type": "string", "region": _region("〇〇株式会社")}])
    assert [w["field"] for w in first.json["warnings"]] == ["issuer_name"]
    v1 = ctx.client.get("/v1/schemas/invoice", headers=auth("admin")).json()
    again = _put(ctx, v1["fields"])  # 取得した fields をそのまま送り返す（編集モード）
    assert [w["field"] for w in again.json["warnings"]] == ["issuer_name"]
    cleared = [{**v1["fields"][0], "region": {**v1["fields"][0]["region"], "example_value": None}}]
    assert _put(ctx, cleared).json["warnings"] == []


def test_除外領域の例示値は警告しない(ctx: SimpleNamespace) -> None:
    """例示値は読取領域でしか使わない（除外領域に付いていても無害で、検証も通る）。"""
    r = ctx.client.put(
        "/v1/schemas",
        headers=auth("admin"),
        json={
            "doc_type": "invoice",
            "fields": [{"name": "a", "type": "string"}],
            "exclude_regions": [{"page": None, "rect": RECT, "example_value": "〇〇株式会社"}],
        },
    )
    assert r.status_code == 200
    assert r.json()["warnings"] == []
