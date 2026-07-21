"""5 フィールド cron（分 時 日 月 曜日）の照合（§16 P8 / source.schedule）。

依存を増やさない自前実装（croniter は不採用）。Vixie cron の仕様に従う:
- 曜日は 0 と 7 の両方が日曜（Python の weekday() は月曜=0 なので変換する）
- 「日」と「曜日」が**両方**制限されている場合は OR で判定（どちらかが合えば発火）
- サポート: `*` / 数値 / リスト `a,b` / 範囲 `a-b` / 刻み `*/n` `a-b/n`

タイムゾーンは **JST（UTC+9・DST なし）固定**。cron を書くのは日本の運用者で、
「毎朝 9 時」が JST の 9 時を意味しないと事故になる。評価側（trigger consumer）は
UTC の現在時刻を JST へ変換してから照合する。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))

_RANGES = {  # フィールドごとの許容値域
    0: (0, 59),  # 分
    1: (0, 23),  # 時
    2: (1, 31),  # 日
    3: (1, 12),  # 月
    4: (0, 7),  # 曜日（0/7=日）
}


class CronError(ValueError):
    pass


def _parse_field(field: str, lo: int, hi: int) -> set[int]:
    out: set[int] = set()
    for part in field.split(","):
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            if not step_s.isdigit() or int(step_s) < 1:
                raise CronError(f"刻みが不正です: {step_s!r}")
            step = int(step_s)
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            if not (a.isdigit() and b.isdigit()):
                raise CronError(f"範囲が不正です: {part!r}")
            start, end = int(a), int(b)
        elif part.isdigit():
            start = end = int(part)
        else:
            raise CronError(f"フィールドが不正です: {part!r}")
        if not (lo <= start <= hi and lo <= end <= hi and start <= end):
            raise CronError(f"値域外です（{lo}-{hi}）: {part!r}")
        out.update(range(start, end + 1, step))
    return out


def parse_cron(expr: str) -> tuple[set[int], set[int], set[int], set[int], set[int], bool, bool]:
    """cron 式を各フィールドの許容集合へ展開する。戻り値の末尾 2 つは
    「日 / 曜日 フィールドが制限されているか」（Vixie の OR 判定に使う）。"""
    fields = expr.split()
    if len(fields) != 5:
        raise CronError(f"cron は 5 フィールドです: {expr!r}")
    sets = []
    for i, f in enumerate(fields):
        lo, hi = _RANGES[i]
        sets.append(_parse_field(f, lo, hi))
    minute, hour, dom, month, dow = sets
    if 7 in dow:  # 0 と 7 は両方日曜
        dow = dow | {0}
    return (
        minute, hour, dom, month, dow,
        fields[2] != "*",
        fields[4] != "*",
    )


def cron_matches(expr: str, at: datetime) -> bool:
    """JST の分単位時刻 at が cron 式に一致するか。at は tz-aware であること。"""
    local = at.astimezone(JST)
    minute, hour, dom, month, dow, dom_limited, dow_limited = parse_cron(expr)
    if local.minute not in minute or local.hour not in hour or local.month not in month:
        return False
    dom_ok = local.day in dom
    dow_ok = (local.isoweekday() % 7) in dow  # 日曜=0 へ変換
    if dom_limited and dow_limited:
        return dom_ok or dow_ok  # Vixie: 両方制限時は OR
    return dom_ok and dow_ok
