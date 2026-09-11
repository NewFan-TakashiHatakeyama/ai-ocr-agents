"""表記揺れを同一視するための照合キー（設計 region-field-add-and-hint-v2 D18）。

同じ目的の正規化が golden に 3 つ（``metrics._norm`` / ``region_ab._norm`` /
``region_from_gold.key``）別々にあった。読取領域ヒントの例示値の照合で 4 つ目を
書く前に、ここへ 1 つに寄せる。orchestrator（例示値と候補 span の突き合わせ）と
golden（計測の正解照合・正解値の位置探索）が**同じ規則**で比べることに意味がある
── 規則がずれると「計測では一致したのに実行時は example_present が立たない」
という、原因の追いにくい差になる。

**値の正規化（§5.6 の正規化器）ではない。** 比較のためだけのキーで、出力を
値として使ってはならない（敬称も通貨記号も落ちている）。
"""

from __future__ import annotations

import unicodedata
from typing import Optional

# 落とす記号: 桁区切り・通貨記号・単位・各種ハイフン/長音/中黒。
# 実測で抽出値と正解の差の大半だった「395,217 vs ￥395,217」「１８丁目 vs 18丁目」
# 「大熊和一様 vs 大熊 和一」を同一視するためのもの（golden/region_ab の経緯）。
_STRIP_CHARS = (",", "￥", "¥", "円", "-", "−", "－", "ー", "‐", "･", "・")

# 宛名の敬称。紙面には付くが「誰宛か」の判定には関係しない。**末尾だけ**落とす
# （「様」を無条件に消すと「お客様各位」のような語まで壊れる）。
_HONORIFIC_SUFFIXES = ("様", "御中", "殿", "行", "宛")


def norm_key(s: Optional[str]) -> str:
    """照合キーを返す。None は空文字。

    規則（順に適用）:
      1. NFKC（全角英数・半角カナ・㈱ 等を揃える）
      2. 空白（全角含む）を全て除く
      3. ``_STRIP_CHARS`` を除く
      4. 末尾の敬称（様 / 御中 / 殿 / 行 / 宛）を除く（この順に各 1 回ずつ判定。
         golden/region_ab の元の規則と同一）
      5. casefold
    """
    if s is None:
        return ""
    t = unicodedata.normalize("NFKC", str(s))
    t = "".join(t.split())
    for ch in _STRIP_CHARS:
        t = t.replace(ch, "")
    for suffix in _HONORIFIC_SUFFIXES:
        if t.endswith(suffix):
            t = t[: -len(suffix)]
    return t.casefold()
