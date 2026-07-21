"""cron 照合（§16 P8）。Vixie 規則（0/7=日曜・日/曜日の OR）と JST 評価を固定する。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from newfan_workflow.cron import JST, CronError, cron_matches, parse_cron


def _jst(y: int, mo: int, d: int, h: int, mi: int) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=JST)


def test_毎分と固定時刻() -> None:
    assert cron_matches("* * * * *", _jst(2026, 7, 21, 9, 0))
    assert cron_matches("0 9 * * *", _jst(2026, 7, 21, 9, 0))
    assert not cron_matches("0 9 * * *", _jst(2026, 7, 21, 9, 1))
    assert not cron_matches("0 9 * * *", _jst(2026, 7, 21, 10, 0))


def test_評価はJSTで行う() -> None:
    # UTC 0:00 = JST 9:00。UTC のまま評価すると「毎朝 9 時」が日本の 18 時になる事故
    utc = datetime(2026, 7, 21, 0, 0, tzinfo=timezone.utc)
    assert cron_matches("0 9 * * *", utc)
    assert not cron_matches("0 0 * * *", utc)


def test_リストと範囲と刻み() -> None:
    assert cron_matches("*/15 * * * *", _jst(2026, 7, 21, 3, 45))
    assert not cron_matches("*/15 * * * *", _jst(2026, 7, 21, 3, 50))
    assert cron_matches("0 9,17 * * *", _jst(2026, 7, 21, 17, 0))
    assert cron_matches("0 9-11 * * *", _jst(2026, 7, 21, 10, 0))
    assert not cron_matches("0 9-11 * * *", _jst(2026, 7, 21, 12, 0))


def test_曜日は0と7が両方日曜() -> None:
    sunday = _jst(2026, 7, 26, 9, 0)  # 2026-07-26 は日曜
    monday = _jst(2026, 7, 27, 9, 0)
    assert cron_matches("0 9 * * 0", sunday)
    assert cron_matches("0 9 * * 7", sunday)
    assert not cron_matches("0 9 * * 0", monday)
    assert cron_matches("0 9 * * 1", monday)


def test_日と曜日が両方制限ならOR判定() -> None:
    # Vixie cron: 2026-07-21 は火曜（dow=2）。日=1 は外れるが曜日=2 が合う → 発火
    tue_21st = _jst(2026, 7, 21, 9, 0)
    assert cron_matches("0 9 1 * 2", tue_21st)
    # どちらも外れる → 発火しない
    assert not cron_matches("0 9 1 * 3", tue_21st)
    # 日だけ制限（曜日 *）なら AND（=日で判定）
    assert not cron_matches("0 9 1 * *", tue_21st)
    assert cron_matches("0 9 21 * *", tue_21st)


def test_不正な式は落ちる() -> None:
    for bad in ("* * * *", "60 * * * *", "* 24 * * *", "x * * * *", "*/0 * * * *"):
        with pytest.raises(CronError):
            parse_cron(bad)
