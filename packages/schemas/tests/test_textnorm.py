"""照合キー norm_key（設計 region-field-add-and-hint-v2 D18）。

golden の 3 つの正規化関数を統合したもの。固定するのは「表記の揺れは同一視し、
値の取り違えは同一視しない」という境界。
"""

from __future__ import annotations

import pytest

from newfan_schemas import norm_key


@pytest.mark.parametrize(
    ("a", "b"),
    [
        # 敬称（末尾）
        ("大熊 和一", "大熊和一様"),
        ("株式会社山田製作所", "株式会社山田製作所 御中"),
        ("山田太郎", "山田太郎 殿"),
        # 全角半角・空白
        ("美しが丘１８丁目５番地２号", "美しが丘 18丁目5番地2号"),
        ("ＡＢＣ", "abc"),
        ("大熊　和一", "大熊 和一"),  # 全角スペース
        # 記号・通貨・桁区切り・ハイフン類
        ("395217", "￥395,217"),
        ("58300", "58,300円"),
        ("395217", "¥395,217-"),
        ("0312345678", "03-1234-5678"),
        ("0312345678", "03−1234−5678"),  # U+2212
        ("0312345678", "03－1234－5678"),  # 全角
        ("ホム", "ホーム"),  # 長音も落とす（region_ab の元規則どおり）
        ("千曲川ホム", "千曲川・ホーム"),
        # casefold
        ("inc.", "INC."),
        # ㈱ は NFKC で (株) になる
        ("(株)山田", "㈱山田"),
    ],
)
def test_表記の揺れは同一視する(a: str, b: str) -> None:
    assert norm_key(a) == norm_key(b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("【サンプル】ビズリフォーム株式会社", "【サンプル】ビズリフォー"),
        ("わくわく物産株式会社", "株式会社エイビーエム"),
        ("395217", "359289"),
        ("株式会社千曲川ホーム", "大熊邸"),
    ],
)
def test_値の取り違えは同一視しない(a: str, b: str) -> None:
    assert norm_key(a) != norm_key(b)


def test_敬称は末尾だけ落とす() -> None:
    """「様」を無条件に消すと語の途中まで壊れる。"""
    assert norm_key("お客様各位") == "お客様各位"
    assert norm_key("様") == ""


def test_None_と空は空文字() -> None:
    assert norm_key(None) == ""
    assert norm_key("") == ""
    assert norm_key("   ") == ""
