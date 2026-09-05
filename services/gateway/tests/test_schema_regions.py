"""領域指定テンプレート化の保存契約（設計 docs/design/region-template-editor.md §4）。

この機能の生命線は **応答忠実性と引き継ぎ** の 2 点である。

- pydantic v2 の既定は ``extra="ignore"`` なので、DTO に 1 つでも書き忘れると
  GET の応答から region が静かに消え、旧編集画面の「取得 → 編集 → 新版として保存」
  往復で設定が全滅する。ここでは往復の完全一致を assert してそれを検知する。
- ``put_schema`` は常に全置換の新版 INSERT なので、exclude_regions を送らない
  旧経路（旧編集画面 / chat）の保存 1 回で除外設定が消えてはならない。
  「省略 = 直前版から引き継ぎ / 明示 [] = クリア」を守る。
"""

from __future__ import annotations

from types import SimpleNamespace

from gw_helpers import auth

from newfan_gateway.db import schema_fields_payload
from newfan_gateway.records import SchemaFieldDef

R_TITLE = {"page": 1, "rect": [0.30, 0.02, 0.72, 0.09]}
R_TOTAL = {"page": "last", "rect": [0.65, 0.80, 0.95, 0.88]}
X_STAMP = {"page": None, "rect": [0.82, 0.02, 0.98, 0.14], "label": "社印"}
X_APPROVAL = {"page": "last", "rect": [0.05, 0.90, 0.30, 0.98], "label": "承認印"}


def _put(ctx: SimpleNamespace, body: dict) -> SimpleNamespace:
    r = ctx.client.put("/v1/schemas", headers=auth("admin"), json=body)
    return SimpleNamespace(status=r.status_code, json=r.json())


def _get(ctx: SimpleNamespace, doc_type: str = "invoice") -> dict:
    r = ctx.client.get(f"/v1/schemas/{doc_type}", headers=auth("admin"))
    assert r.status_code == 200
    return r.json()


# ---------- 往復（DTO 落ちの検知） ----------


def test_put_get_roundtrip_region_and_exclude(ctx: SimpleNamespace) -> None:
    res = _put(
        ctx,
        {
            "doc_type": "invoice",
            "fields": [
                {"name": "title", "label": "帳票タイトル", "type": "string", "region": R_TITLE},
                {"name": "total", "type": "money_jpy", "critical": True, "region": R_TOTAL},
            ],
            "exclude_regions": [X_STAMP, X_APPROVAL],
            "source_page_count": 2,
        },
    )
    assert res.status == 200
    # PUT 応答と GET 応答が一致すること（片方だけ DTO を通していると差が出る）
    got = _get(ctx)
    for payload in (res.json, got):
        by_name = {f["name"]: f for f in payload["fields"]}
        assert by_name["title"]["region"]["page"] == 1
        assert by_name["title"]["region"]["rect"] == [0.30, 0.02, 0.72, 0.09]
        assert by_name["total"]["region"]["page"] == "last"
        assert payload["source_page_count"] == 2
        assert [x["label"] for x in payload["exclude_regions"]] == ["社印", "承認印"]
        assert payload["exclude_regions"][0]["page"] is None


def test_old_schema_returns_null_region_empty_excludes(ctx: SimpleNamespace) -> None:
    """0007 以前に作られた版は region なし・exclude 空で返る（後方互換）。"""
    got = _get(ctx)  # conftest が seed した v4（region を知らない）
    assert all(f.get("region") is None for f in got["fields"])
    assert got["exclude_regions"] == []
    assert got["source_page_count"] is None


# ---------- 引き継ぎ（旧経路の保存で消えないこと） ----------


def test_legacy_put_without_exclude_key_inherits(ctx: SimpleNamespace) -> None:
    """exclude_regions キーを含まない旧形式 body で新版を作っても設定が残る。

    旧編集画面（web/lib/api.ts の従来 putSchema）と chat の update_schema は
    ``{doc_type, fields, create}`` しか送らない。ここが「省略時 []」だと
    旧画面での保存 1 回で除外設定が全滅する（設計 §4.4）。
    """
    _put(
        ctx,
        {
            "doc_type": "invoice",
            "fields": [{"name": "total", "type": "money_jpy"}],
            "exclude_regions": [X_STAMP],
            "source_page_count": 3,
        },
    )
    # 旧形式（exclude_regions / source_page_count のキー自体が無い）
    legacy = _put(
        ctx,
        {"doc_type": "invoice", "fields": [{"name": "total", "type": "money_jpy"}]},
    )
    assert legacy.status == 200
    # PUT 応答と GET の両方で引き継がれていること。PUT 応答が [] だと旧画面は
    # 「消えた」state を持ち、次の保存で明示 []（本当のクリア）を送ってしまう。
    assert legacy.json["exclude_regions"] == [X_STAMP]
    assert legacy.json["source_page_count"] == 3
    got = _get(ctx)
    assert got["exclude_regions"] == [X_STAMP]
    assert got["source_page_count"] == 3


def test_explicit_empty_clears(ctx: SimpleNamespace) -> None:
    """明示 [] だけがクリアを意味する。"""
    _put(
        ctx,
        {
            "doc_type": "invoice",
            "fields": [{"name": "total", "type": "money_jpy"}],
            "exclude_regions": [X_STAMP],
        },
    )
    cleared = _put(
        ctx,
        {
            "doc_type": "invoice",
            "fields": [{"name": "total", "type": "money_jpy"}],
            "exclude_regions": [],
        },
    )
    assert cleared.status == 200
    assert cleared.json["exclude_regions"] == []
    assert _get(ctx)["exclude_regions"] == []


def test_chat_update_schema_preserves_regions(ctx: SimpleNamespace) -> None:
    """chat の項目追加ツールは region / exclude を知らないが、失わない。"""
    _put(
        ctx,
        {
            "doc_type": "invoice",
            "fields": [{"name": "title", "type": "string", "region": R_TITLE}],
            "exclude_regions": [X_STAMP],
            "source_page_count": 2,
        },
    )
    from newfan_gateway.chat_tools import ChatTools

    tools = ChatTools(repo=ctx.repo, admin=ctx.admin, queue=ctx.queue)
    res = tools.update_schema("ten_1", "invoice", {"name": "memo", "type": "string"})
    assert res["ok"] is True

    got = _get(ctx)
    by_name = {f["name"]: f for f in got["fields"]}
    assert by_name["title"]["region"]["rect"] == [0.30, 0.02, 0.72, 0.09]
    assert by_name["memo"]["region"] is None
    assert got["exclude_regions"] == [X_STAMP]
    assert got["source_page_count"] == 2


def test_edit_mode_roundtrip_preserves_required_critical_columns(ctx: SimpleNamespace) -> None:
    """編集モード相当の body（元フィールドをスプレッド）で属性が往復すること。

    新プレビューは required / critical / columns を編集しないが、``put_schema`` は
    常に全置換なので、送らなければ新版で消える。UI 側は元フィールドを丸ごと
    引き継ぐ規約（§3.3 base スプレッド）であり、この経路が壊れていないことを
    サーバ側からも押さえる。
    """
    _put(
        ctx,
        {
            "doc_type": "invoice",
            "fields": [
                {"name": "total", "type": "money_jpy", "required": True, "critical": True},
                {
                    "name": "lines",
                    "type": "table",
                    "columns": [{"name": "item", "type": "string"}],
                },
            ],
        },
    )
    v1 = _get(ctx)
    # 「矩形だけ足す」= 取得した fields をそのまま送り返し、1 つに region を付ける
    fields = [dict(f) for f in v1["fields"]]
    fields[0]["region"] = R_TOTAL
    v2 = _put(ctx, {"doc_type": "invoice", "fields": fields})
    assert v2.status == 200

    got = _get(ctx)
    by_name = {f["name"]: f for f in got["fields"]}
    assert by_name["total"]["required"] is True and by_name["total"]["critical"] is True
    assert by_name["total"]["region"]["page"] == "last"
    assert by_name["lines"]["type"] == "table"
    assert by_name["lines"]["columns"] == [{"name": "item", "type": "string"}]


# ---------- 検証（422） ----------


def test_rect_validation_422(ctx: SimpleNamespace) -> None:
    base = {"doc_type": "v", "fields": [{"name": "a", "type": "string"}]}

    # 範囲外
    r = _put(ctx, {**base, "exclude_regions": [{"page": 1, "rect": [0.1, 0.1, 1.4, 0.5]}]})
    assert r.status == 422
    # x1 >= x2
    r = _put(ctx, {**base, "exclude_regions": [{"page": 1, "rect": [0.5, 0.1, 0.5, 0.5]}]})
    assert r.status == 422
    # 要素数不足
    r = _put(ctx, {**base, "exclude_regions": [{"page": 1, "rect": [0.1, 0.1, 0.5]}]})
    assert r.status == 422
    # 面積ゼロ同然（誤クリック由来）
    r = _put(ctx, {**base, "exclude_regions": [{"page": 1, "rect": [0.1, 0.1, 0.101, 0.101]}]})
    assert r.status == 422
    # page が 0 以下
    r = _put(ctx, {**base, "exclude_regions": [{"page": 0, "rect": [0.1, 0.1, 0.5, 0.5]}]})
    assert r.status == 422


def test_include_region_requires_page(ctx: SimpleNamespace) -> None:
    """読取領域に page:null は許さない（全ページ指定は除外領域のみ）。

    形式検証は pydantic が担うが、これは文脈依存の制約なので put_schema で見る。
    """
    r = _put(
        ctx,
        {
            "doc_type": "v",
            "fields": [
                {"name": "a", "type": "string", "region": {"rect": [0.1, 0.1, 0.5, 0.5]}}
            ],
        },
    )
    assert r.status == 422
    assert r.json["error"]["code"] == "E1003"
    # 除外側は page:null が正当
    ok = _put(
        ctx,
        {
            "doc_type": "v",
            "fields": [{"name": "a", "type": "string"}],
            "exclude_regions": [{"rect": [0.1, 0.1, 0.5, 0.5]}],
        },
    )
    assert ok.status == 200
    assert ok.json["exclude_regions"][0]["page"] is None


# ---------- JSONB 直列化（プロンプト同一性の土台） ----------


def test_put_without_region_stores_no_region_key() -> None:
    """region 未設定の field は JSONB に ``region`` キー自体を書かない。

    ``"region": null`` が入ると、旧 orchestrator が schema を丸ごと json.dumps で
    プロンプトへ載せるため、領域を使っていないスキーマでも kie プロンプトが変わる
    （＝抽出結果が変わり得る）。gateway と orchestrator-worker のローリング完了順は
    保証されないので、順序に依存しない形でこれを閉じる（設計 §4.7）。
    """
    from newfan_schemas import RegionRect

    rows = schema_fields_payload(
        [
            SchemaFieldDef(name="a", type="string"),
            SchemaFieldDef(name="b", type="string", region=RegionRect(**R_TITLE)),
        ]
    )
    assert "region" not in rows[0]
    # region 以外の null は現行どおり残す（消すと現行プロンプトが変わってしまう）
    assert rows[0]["label"] is None and rows[0]["columns"] is None
    assert rows[1]["region"]["page"] == 1


# ---------- ページ寸法（GET /documents/{id}） ----------


def test_document_detail_returns_page_dims(ctx: SimpleNamespace) -> None:
    up = ctx.client.post(
        "/v1/documents",
        headers=auth("admin"),
        files={"file": ("a.png", b"\x89PNG\r\n\x1a\n" + b"0" * 64, "image/png")},
    )
    assert up.status_code == 201
    doc_id = up.json()["document_id"]

    r = ctx.client.get(f"/v1/documents/{doc_id}", headers=auth("viewer"))
    assert r.status_code == 200
    pages = r.json()["pages"]
    assert [p["page_no"] for p in pages] == sorted(p["page_no"] for p in pages)
    assert len(pages) == r.json()["page_count"]
    assert all("width" in p and "height" in p for p in pages)


def test_document_list_does_not_fill_page_dims(ctx: SimpleNamespace) -> None:
    """一覧は pages を埋めない（帳票ごとに pages を引く N+1 を避ける。§6 / C25）。"""
    ctx.client.post(
        "/v1/documents",
        headers=auth("admin"),
        files={"file": ("a.png", b"\x89PNG\r\n\x1a\n" + b"0" * 64, "image/png")},
    )
    r = ctx.client.get("/v1/documents", headers=auth("viewer"))
    assert r.status_code == 200
    assert r.json()["items"], "一覧が空だとこのテストは何も検証していない"
    assert all(item["pages"] == [] for item in r.json()["items"])
