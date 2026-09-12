"""「引数を渡さなかった」を None と区別する印（依存の無い最小モジュール）。

admin.py（Protocol / InMemory）と db.py（Pg）の両方がモジュール先頭で import する。
db.py は循環 import を避けるために admin を関数内でしか import できないので、
印そのものは第三のモジュールに置く。
"""

from __future__ import annotations

from typing import Any


class _Unset:
    """put_schema の ``source_page_count`` などで「省略」を表す。

    **省略 = 直前版から引き継ぎ / 明示 None = クリア**（設計 region-template-editor
    §4.4。exclude_regions の「省略 = 引き継ぎ / [] = クリア」と対）。None を引き継ぎの
    印に使うと NULL に戻す手段が無くなり、誤って記録した値（寸法未登録時の 1 など）を
    API から直せない（第 3 回敵対的レビュー 7）。
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "UNSET"

    def __bool__(self) -> bool:
        return False


UNSET: Any = _Unset()
