"""記入例語（placeholder_example）の判定（設計 region-field-add-and-hint-v2 §2.5）。

判定は ``newfan_schemas.placeholder`` にあり、orchestrator の事前ガード（``prevalidate``）と
gateway の保存時の警告（``PUT /schemas`` の応答・``POST /schemas/example-values/check``）が
**同じ判定**を使う。PR #16 で ``services/orchestrator/tests/test_llm_nodes.py`` に置いた
判定そのもののテストを、判定の移設に合わせてここへ移した（入力も期待値も 1 件も変えていない）。
ヒントを落とす経路（``build_region_hints`` 経由）のテストは orchestrator 側に残る。
"""

from __future__ import annotations

import json

import pytest

from newfan_schemas import is_placeholder_example


@pytest.mark.parametrize(
    "text",
    [
        # 第 4 回計測 S3 で害を出した sample13 の 3 項目（原文どおり・region_from_gold の出力どおり）
        "自社名(ロゴや社判も登録できます)",
        "自社名（ロゴや社判も登録できます)",
        "東京都千代田区住所1住所2ビル名等",
        "東京都千代田区住所1 住所2ビル名等",
        "得意先の会社名",
        # 見出し語だけ（助詞・記号・連番 1 桁を除いて何も残らない）
        "会社名",
        "氏名",
        "ご担当者名",
        "担当者名1",
        "お客様名 様",
        "貴社名：",
        "Company Name",
        "Your Company Inc.",
        # 強い印
        "都道府県市区町村番地",
        "ビル名など",
        "ここに社名を入力",
        "記入例",
        "Address Line 1",
        "lorem ipsum dolor",
        # 伏せ字だけ（法人格・敬称・住所の単位字・元号・通貨・数字・区切りを剥がして）
        "〇〇株式会社",
        "○○ 様",
        "△△△",
        "〒○○○-○○○○",
        "○○県○○市○○町1-2-3",
        "令和○年○月○日",
        "¥○○○",
        "株式会社○○ 代表取締役 ○○ ○○",
        "www.○○.co.jp",
        "____________",
        # X は残りが X と伏せ字だけのとき
        "03-XXXX-XXXX",
        "20XX年XX月XX日",
        "XXXX-XXXX",
        "$XX,XXX.XX",
        # 第 5 回の敵対的検証（4 レンズ）で拾った記入例語
        "株式会社",
        "〒",
        "見本",
        "SAMPLE",
        "テスト株式会社",
        "ダミー株式会社",
        "サンプル 太郎",
        "自由記入欄",
        "氏名（フリガナ）",
        "合計金額（税込）",
        "例）株式会社〇〇",
        "例：山田太郎",
        "令和 年 月 日",
        "YYYY/MM/DD",
        "商品A",
        "〒000-0000",
        "000-0000-0000",
        "¥000,000",
        "[Insert name]",
        "{{customer.name}}",
        "${InvoiceNumber}",
        "Your Logo Here",
        "Click here to enter text.",
        "Bill To:",
        "sample@example.com",
        "https://example.com",
        "(555) 555-5555",
        "TBD",
        "Musterfirma GmbH",
        # 見出し語＋伏せ字（規則 2 と 3 の合成。コードレビューで見つけた欠落）
        "会社名 〇〇株式会社",
        "TEL ○○-○○○○-○○○○",
        "担当者名 〇〇",
        "〒○○○−○○○○",  # U+2212 MINUS SIGN（IME の「−」は NFKC で - にならない）
        "E-mail",
        "電話",
        "メールアドレス",
        "請求先",
        "番号 1",
        "住所 2",
    ],
)
def test_placeholder_example_記入例語は落とす(text: str) -> None:
    assert is_placeholder_example(text) is True


@pytest.mark.parametrize(
    "text",
    [
        # 紙面にそのまま印字された「サンプルらしい」値は値（種類も判定できる）
        "【サンプル】ビズリフォーム株式会社",
        "サンプル商事株式会社",
        "東京都新宿区高田馬場XX-X-XX新宿ビルX階",
        "名古屋市中区○-○-○○○生命ビル12F",
        "東京都サンプル区サンプル1-2-3",
        "sample.Inc",
        "A-123 Sample.BLD,1-2-3,sample-city",
        "DEMO.co.,ltd",
        # 見出し語を含む実社名・実住所（残りに値がある）
        "株式会社名古屋製作所",
        "株式会社ロゴスコーポレーション",
        "会社名 株式会社山田",
        "氏名 山田太郎",
        "TEL 03-1234-5678",
        "住所 東京都新宿区四谷0-0-0",
        "御中" + " 株式会社エイビーエム",
        # 第 5 回の敵対的検証で「実在しうる」と確かめた紛らわしい値
        "テストー株式会社",  # testo の日本法人
        "役職 代表取締役",  # 見出し＋役職の値
        "△12,000",  # 会計の負数
        "▲1,250,000",
        "氏名 李",  # 1 字姓（見出し語＋連番の規則は ASCII に限る）
        "Statesman Consulting LLC",
        "Zipline Co., Ltd.",
        "東京都○○区○○1-2-3",  # 固有の実文字（東京）が残る
        "○-○-○○○生命ビル",
        "03-0000-0000",  # 市外局番が実数字
        "00000101",
        "Anytime Fitness",
        "info@zipline.co.jp",
        # コードレビューで見つけた偽陽性（直した）
        "株式会社判治商店",  # 「社判」は「会社判」を除く（判治・判田は実在する姓）
        "有限会社判田建設",
        "住所 1-2-3",  # 見出し＋番地始まりの値
        "住所 1丁目2番3号",
        "×2",  # 数量の ×
        "2×3",
        "****1234",  # マスク済みの番号
        "****-****-****-1234",
        "00:00",  # 時刻はゼロ埋めではない
        "消費税 0",  # 非課税明細の実値（連番に 0 は無い）
        "小計 0",
        "Total 0",
        "¥▲1,000",
        "Data Center The Hub",  # 英語の強い印は語境界を見る
        "Prototype Here",
        # 普通の値
        "ビズリフォーム株式会社",
        "大熊 和一",
        "グランドコート浦和美園 管理組合 理事長 駒林 次郎",
        "ＡＡＡ食品株式会社 ＡＡＡ支社",
        "SystemBase",
        "395,217",
        "令和5年5月1日",
        "T1234567890123",
        "INV-2024-001",
        "1",
        "No.",
        "",
        None,
    ],
)
def test_placeholder_example_値は落とさない(text: str | None) -> None:
    assert is_placeholder_example(text) is False


def test_placeholder_example_正解データの値は_sample13_の3項目だけが記入例語() -> None:
    """設計に使った sample13 以外の 28 帳票の正解値（別データ）で誤検知が無いこと。
    ここが崩れると正しいテンプレートのヒントを失う（対照に戻るだけだが効き目を失う）。"""
    import pathlib

    path = pathlib.Path(__file__).resolve().parents[3] / "golden" / "data" / "samples_ground_truth.json"
    if not path.exists():
        pytest.skip("golden データが無い")
    docs = json.loads(path.read_text(encoding="utf-8"))["documents"]
    flagged = sorted(
        (name, field)
        for name, d in docs.items()
        for field, value in d["fields"].items()
        if isinstance(value, str) and is_placeholder_example(value)
    )
    assert flagged == [
        ("sample13.png", "customer_name"),
        ("sample13.png", "issuer_address"),
        ("sample13.png", "issuer_name"),
    ]


def test_placeholder_example_第4回の例示値の全件で_sample13_由来だけが落ちる() -> None:
    """例示値の実際の形は「見出し語＋値」の連結（region_from_gold の出力）なので、正解値だけでは
    足りない。第 4 回計測の S2 / S3 の例示値 94 件（golden/data/region_ab_example_values.json）で、
    記入例の帳票 sample13 由来の 3 件以外を落とさないこと。"""
    import pathlib

    path = pathlib.Path(__file__).resolve().parents[3] / "golden" / "data" / "region_ab_example_values.json"
    if not path.exists():
        pytest.skip("golden データが無い")
    items = json.loads(path.read_text(encoding="utf-8"))["items"]
    assert len(items) >= 90
    flagged = sorted((i["doc_type"], i["field"]) for i in items if is_placeholder_example(i["example_value"]))
    assert flagged == [
        ("s3_sample2_from_sample13", "customer_name"),
        ("s3_sample2_from_sample13", "issuer_address"),
        ("s3_sample2_from_sample13", "issuer_name"),
    ]
