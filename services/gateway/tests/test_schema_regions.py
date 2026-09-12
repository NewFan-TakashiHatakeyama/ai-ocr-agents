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


def _stored(region: dict) -> dict:
    """API 応答での領域の形。読取領域ヒントの 3 項目（設計 region-field-add-and-hint-v2
    §2.3）は送らなくても None で必ず返る。完全一致で比べる（DTO 落ち検知）ために、
    期待値側にその既定値を足す。"""
    return {"example_value": None, "origin": None, "created_at": None, **region}


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


R_HINTED = {
    "page": 1,
    "rect": [0.30, 0.02, 0.72, 0.09],
    "example_value": "株式会社千曲川ホーム",
    "origin": "ghost",
    "created_at": "2026-09-11T00:00:00Z",
}


def test_put_get_roundtrip_region_hint_fields(ctx: SimpleNamespace) -> None:
    """example_value / origin / created_at が PUT 応答と GET の両方で返る。

    設計 region-field-add-and-hint-v2 §2.3。RegionRect は newfan_schemas に一元定義
    で gateway の DTO からも参照されているので「モデルに足せば往復するはず」だが、
    その「はず」を pydantic の extra="ignore" が黙って裏切った前例がある（§4.2）。
    3 項目とも値の完全一致で固定する。
    """
    res = _put(
        ctx,
        {
            "doc_type": "invoice",
            "fields": [
                {"name": "issuer", "label": "取引先名", "type": "string", "region": R_HINTED},
                {"name": "total", "type": "money_jpy", "region": R_TOTAL},  # 3 項目なし
            ],
        },
    )
    assert res.status == 200
    for payload in (res.json, _get(ctx)):
        by_name = {f["name"]: f for f in payload["fields"]}
        hinted = by_name["issuer"]["region"]
        assert hinted["example_value"] == "株式会社千曲川ホーム"
        assert hinted["origin"] == "ghost"
        assert hinted["created_at"] == "2026-09-11T00:00:00Z"
        assert hinted["rect"] == [0.30, 0.02, 0.72, 0.09]
        # 3 項目を送らなかった領域は None で返る（キーが消えるのではない）
        plain = by_name["total"]["region"]
        assert plain["page"] == "last"
        assert plain["example_value"] is None
        assert plain["origin"] is None
        assert plain["created_at"] is None


def test_edit_mode_roundtrip_preserves_hint_fields(ctx: SimpleNamespace) -> None:
    """取得した fields をそのまま送り返す編集モードで 3 項目が新版に引き継がれる。"""
    _put(
        ctx,
        {"doc_type": "invoice", "fields": [{"name": "issuer", "type": "string", "region": R_HINTED}]},
    )
    v1 = _get(ctx)
    v2 = _put(ctx, {"doc_type": "invoice", "fields": v1["fields"]})
    assert v2.status == 200
    got = _get(ctx)
    assert got["version"] == v1["version"] + 1
    region = got["fields"][0]["region"]
    assert (region["example_value"], region["origin"], region["created_at"]) == (
        "株式会社千曲川ホーム",
        "ghost",
        "2026-09-11T00:00:00Z",
    )


def test_put_sanitizes_example_value_and_rejects_bad_origin(ctx: SimpleNamespace) -> None:
    """消毒（制御文字・上限）はモデル側で掛かり、API 応答にはその結果が載る。"""
    res = _put(
        ctx,
        {
            "doc_type": "invoice",
            "fields": [
                {
                    "name": "issuer",
                    "type": "string",
                    "region": {**R_HINTED, "example_value": "  株式会社\x00千曲川\n" + "x" * 300},
                }
            ],
        },
    )
    assert res.status == 200
    ev = res.json["fields"][0]["region"]["example_value"]
    assert ev.startswith("株式会社千曲川x") and len(ev) == 200
    # origin の値域外・created_at の非 ISO は形式検証（pydantic）で 422
    bad_origin = _put(
        ctx,
        {
            "doc_type": "invoice",
            "fields": [{"name": "issuer", "type": "string", "region": {**R_HINTED, "origin": "auto"}}],
        },
    )
    assert bad_origin.status == 422
    bad_ts = _put(
        ctx,
        {
            "doc_type": "invoice",
            "fields": [
                {"name": "issuer", "type": "string", "region": {**R_HINTED, "created_at": "先週"}}
            ],
        },
    )
    assert bad_ts.status == 422


def test_exclude_region_with_hint_fields_is_accepted(ctx: SimpleNamespace) -> None:
    """除外領域に 3 項目が付いていても保存は通る（読取領域でしか使わないが無害）。"""
    res = _put(
        ctx,
        {
            "doc_type": "invoice",
            "fields": [{"name": "a", "type": "string"}],
            "exclude_regions": [{**X_STAMP, "example_value": "印", "origin": "manual"}],
        },
    )
    assert res.status == 200
    assert res.json["exclude_regions"][0]["label"] == "社印"
    assert res.json["exclude_regions"][0]["page"] is None


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
    assert legacy.json["exclude_regions"] == [_stored(X_STAMP)]
    assert legacy.json["source_page_count"] == 3
    got = _get(ctx)
    assert got["exclude_regions"] == [_stored(X_STAMP)]
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
    assert got["exclude_regions"] == [_stored(X_STAMP)]
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


# ---------- 予約名（設計 region-field-add-and-hint-v2 D9） ----------


def test_reserved_field_name_rejected_with_error_envelope(ctx: SimpleNamespace) -> None:
    """``__pages__`` / ``__region__`` / 先頭 ``__`` は 422 かつ**プロジェクトの error 封筒**。

    集約 ReviewItem の擬似 field 名と衝突するとレビュー画面で所見の帰属が壊れる。
    pydantic の 422（``{"detail": [...]}``）ではなく ``{"error": {"code": "E1003"}}``
    で返すのは、web 側のエラー表示がこの封筒しか読まないため。
    """
    before = _get(ctx)["version"]
    for name in ("__pages__", "__region__", "__custom"):
        r = _put(
            ctx,
            {"doc_type": "invoice", "fields": [{"name": name, "type": "string"}]},
        )
        assert r.status == 422, name
        assert set(r.json) == {"error"}, r.json
        assert r.json["error"]["code"] == "E1003"
        assert "予約" in r.json["error"]["message"]
        assert r.json["error"]["details"] == {"field": name}
    # 拒否した PUT は新版を作らない
    assert _get(ctx)["version"] == before
    # 過剰拒否しない: 末尾・途中の __ と単独の _ は通る
    ok = _put(
        ctx,
        {
            "doc_type": "invoice",
            "fields": [{"name": n, "type": "string"} for n in ("x__", "a__b", "_private")],
        },
    )
    assert ok.status == 200


# ---------- 未知の型（ADR-0007 の展開順の事故を保存時に止める） ----------


def test_unknown_field_type_rejected_with_error_envelope(ctx: SimpleNamespace) -> None:
    """``FieldType`` に無い型は 422 かつ E1003 の封筒で拒み、新版を作らない。

    orchestrator は保存されたスキーマを ``FieldSchema``（``type: FieldType``）で検証する
    ので、知らない型が保存されると、その doc_type の抽出が**すべて** failed（E9001）に
    なって初めて分かる。gateway が保存時に拒めば、画面で選んだ型を worker が知らない
    組み合わせ（gateway だけ新しい）はその場で分かる。
    """
    before = _get(ctx)["version"]
    for type_ in ("addres_jp", "text", "ADDRESS_JP"):
        r = _put(ctx, {"doc_type": "invoice", "fields": [{"name": "memo", "type": type_}]})
        assert r.status == 422, type_
        assert set(r.json) == {"error"}, r.json
        assert r.json["error"]["code"] == "E1003"
        assert "型" in r.json["error"]["message"] and type_ in r.json["error"]["message"]
        assert r.json["error"]["details"] == {"field": "memo"}
    assert _get(ctx)["version"] == before
    # 既知の型は通る（address_jp を含む）
    ok = _put(
        ctx,
        {
            "doc_type": "invoice",
            "fields": [
                {"name": "customer_address", "type": "address_jp"},
                {"name": "bank", "type": "jp_bank_account"},
            ],
        },
    )
    assert ok.status == 200


def test_legacy_unknown_field_type_is_readable_but_not_writable(ctx: SimpleNamespace) -> None:
    """型の拒否も書き込み側だけ。読み出しは旧データを通す（予約名と同じ理由）。"""
    from newfan_gateway.records import SchemaFieldDef, SchemaRecord

    ctx.admin.seed_schema(
        SchemaRecord(
            id="sch_legacy_type", tenant_id="ten_1", doc_type="legacy_type", version=1,
            fields=[SchemaFieldDef(name="memo", label="メモ", type="text")],
        )
    )
    assert ctx.client.get("/v1/schemas/legacy_type", headers=auth("admin")).status_code == 200
    r = _put(ctx, {"doc_type": "legacy_type", "fields": [{"name": "memo", "type": "text"}]})
    assert r.status == 422 and r.json["error"]["code"] == "E1003"


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


# ---------- result API（Phase 2） ----------


def _doc_with_run(ctx: SimpleNamespace, *, pages: int, schema_id: str, metrics_region=None) -> str:
    """指定ページ数の帳票と、そのスキーマを指す run を用意する。"""
    from newfan_gateway.records import DocumentRecord, PageRecord, RunRecord

    doc_id = f"doc_test_{pages}_{schema_id[-6:]}"
    ctx.repo.create_document(
        DocumentRecord(
            id=doc_id, tenant_id="ten_1", storage_uri="s3://b/k", mime_type="image/png",
            page_count=pages, status="uploaded",
        ),
        [
            PageRecord(page_no=i, width=1000, height=1400, image_uri=f"s3://b/p{i}.png")
            for i in range(1, pages + 1)
        ],
    )
    run = RunRecord(
        id=f"run_{doc_id}", tenant_id="ten_1", document_id=doc_id,
        schema_id=schema_id, status="needs_review", region_stats=metrics_region,
    )
    ctx.repo.create_run(run)
    return doc_id


def test_result_applied_exclude_regions_resolves_last_and_null(ctx: SimpleNamespace) -> None:
    """"last" と null をサーバ側で解決して返す（設計 §6 / C16）。

    検証画面は自分が描いているページ番号しか持たないので、解決を web に任せると
    「最終ページ限定の承認印除外が全ページに描かれる／描かれない」事故になる。
    ページ数を超える指定は落とす（別ページを消していると誤解させない）。
    """
    sch = _put(
        ctx,
        {
            "doc_type": "resolve_probe",
            "fields": [{"name": "a", "type": "string"}],
            "exclude_regions": [
                X_APPROVAL,                                        # page: "last"
                X_STAMP,                                           # page: null
                {"page": 9, "rect": [0.1, 0.1, 0.2, 0.2]},         # 存在しないページ
            ],
            "create": True,
        },
    ).json
    doc_id = _doc_with_run(ctx, pages=3, schema_id=sch["id"])

    body = ctx.client.get(f"/v1/documents/{doc_id}/result", headers=auth("viewer")).json()
    applied = body["applied_exclude_regions"]
    by_label = sorted((r["page_no"], r["label"]) for r in applied)
    assert by_label == [(1, "社印"), (2, "社印"), (3, "承認印"), (3, "社印")]
    assert all(r["rect"] for r in applied)
    # InMemory 実装でも非空であること（本番との差に気づけない盲点を作らない）
    assert applied


def test_result_exposes_schema_doc_type_and_region_stats(ctx: SimpleNamespace) -> None:
    """編集モードのプリロード起点（doc_type）と除外バッジの材料を返す。"""
    sch = _put(
        ctx,
        {"doc_type": "stats_probe", "fields": [{"name": "a", "type": "string"}], "create": True},
    ).json
    doc_id = _doc_with_run(
        ctx, pages=1, schema_id=sch["id"],
        metrics_region={"excluded_spans": 4, "excluded_cells": 2, "excluded_rows": 1},
    )
    body = ctx.client.get(f"/v1/documents/{doc_id}/result", headers=auth("viewer")).json()
    assert body["schema_doc_type"] == "stats_probe"
    assert body["region_stats"]["excluded_cells"] == 2


def test_result_schemaless_run_has_empty_region_fields(ctx: SimpleNamespace) -> None:
    """スキーマレス run では空（テンプレートレス運用に退行なし）。"""
    from newfan_gateway.records import DocumentRecord, PageRecord, RunRecord

    ctx.repo.create_document(
        DocumentRecord(
            id="doc_bare", tenant_id="ten_1", storage_uri="s3://b/k", mime_type="image/png",
            page_count=1, status="uploaded",
        ),
        [PageRecord(page_no=1, width=100, height=100, image_uri="s3://b/p1.png")],
    )
    ctx.repo.create_run(
        RunRecord(id="run_bare", tenant_id="ten_1", document_id="doc_bare", status="needs_review")
    )
    body = ctx.client.get("/v1/documents/doc_bare/result", headers=auth("viewer")).json()
    assert body["applied_exclude_regions"] == []
    assert body["schema_doc_type"] is None
    assert body["region_stats"] is None


def test_result_は位置ガードの所見を素通しで返す(ctx: SimpleNamespace) -> None:
    """mismatch_fields / layout_mismatch を検証画面まで届ける唯一の経路。

    region_stats は `dict[str, Any]` の素通しなので、将来ここを型付き DTO に
    絞る変更が入ると **UI 側は静かに何も表示しなくなる**（pydantic v2 の既定は
    extra="ignore"）。落ちるようにしておく。
    """
    sch = _put(
        ctx,
        {
            "doc_type": "guard_view_probe",
            "fields": [{"name": "total_amount", "type": "money_jpy"}],
            "create": True,
        },
    ).json
    doc_id = _doc_with_run(
        ctx,
        pages=1,
        schema_id=sch["id"],
        metrics_region={
            "excluded_spans": 0,
            "mismatch_fields": ["total_amount", "issuer_name"],
            "layout_mismatch": False,
        },
    )
    body = ctx.client.get(f"/v1/documents/{doc_id}/result", headers=auth("viewer")).json()
    assert body["region_stats"]["mismatch_fields"] == ["total_amount", "issuer_name"]
    assert body["region_stats"]["layout_mismatch"] is False


def test_result_の所見なしはキー自体が無い(ctx: SimpleNamespace) -> None:
    """0 件と「そもそも判定していない」を UI が取り違えないよう、キーの不在を固定する。"""
    sch = _put(
        ctx,
        {
            "doc_type": "guard_none_probe",
            "fields": [{"name": "total_amount", "type": "money_jpy"}],
            "create": True,
        },
    ).json
    doc_id = _doc_with_run(
        ctx, pages=1, schema_id=sch["id"], metrics_region={"excluded_spans": 4}
    )
    body = ctx.client.get(f"/v1/documents/{doc_id}/result", headers=auth("viewer")).json()
    assert "mismatch_fields" not in body["region_stats"]
    assert "layout_mismatch" not in body["region_stats"]


def test_legacy_reserved_field_name_is_readable_but_not_writable(ctx: SimpleNamespace) -> None:
    """予約名の拒否は書き込み側だけ。読み出しは旧データを通す（InMemory 版）。

    実 Pg 版は test_pg_repository_integration.py。ここでは InMemory の
    seed_schema で「検査導入前に入った行」を再現し、GET が落ちないことと、
    PUT では拒むことを固定する。
    """
    from newfan_gateway.records import SchemaFieldDef, SchemaRecord

    ctx.admin.seed_schema(
        SchemaRecord(
            id="sch_legacy", tenant_id="ten_1", doc_type="legacy_memo", version=1,
            fields=[SchemaFieldDef(name="__memo", label="メモ", type="string")],
        )
    )
    r = ctx.client.get("/v1/schemas", headers=auth("admin"))
    assert r.status_code == 200, r.text
    assert "legacy_memo" in {s["doc_type"] for s in r.json()["items"]}
    assert ctx.client.get("/v1/schemas/legacy_memo", headers=auth("admin")).status_code == 200
    # 書き込みは拒む
    r = _put(ctx, {"doc_type": "legacy_memo", "fields": [{"name": "__memo", "type": "string"}]})
    assert r.status == 422 and r.json["error"]["code"] == "E1003"
