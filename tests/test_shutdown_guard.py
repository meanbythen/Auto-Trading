"""自动关机守卫：周末与未平仓位都不应该关机。"""

from __future__ import annotations

from datetime import datetime

from src.utils.shutdown import ready_for_shutdown

SESSIONS = [["09:00", "15:00"], ["21:00", "23:00"]]
FLATS = ["14:55", "22:55"]


def test_weekend_never_shuts_down() -> None:
    ok, why = ready_for_shutdown(
        sessions=SESSIONS,
        flatten_times=FLATS,
        now=datetime(2026, 11, 7, 23, 10),  # 周六
        has_position=False,
    )
    assert ok is False
    assert "周末" in why


def test_position_blocks_shutdown() -> None:
    ok, why = ready_for_shutdown(
        sessions=SESSIONS,
        flatten_times=FLATS,
        now=datetime(2026, 11, 2, 23, 10),  # 周一
        has_position=True,
    )
    assert ok is False
    assert "未平仓位" in why


def test_after_close_without_position_can_shut_down() -> None:
    ok, _ = ready_for_shutdown(
        sessions=SESSIONS,
        flatten_times=FLATS,
        now=datetime(2026, 11, 2, 23, 10),
        has_position=False,
    )
    assert ok is True


def test_overnight_close_is_later_than_afternoon() -> None:
    """白银夜盘收到凌晨时，下午 15:00 不能当成当天最后收盘。"""
    from datetime import time as dtime

    from src.utils.shutdown import last_close_time

    close = last_close_time(
        [["09:00", "15:00"], ["21:00", "02:30"]],
        ["14:55", "02:25"],
    )
    assert close == dtime(2, 30)


def test_before_close_does_not_shut_down() -> None:
    ok, _ = ready_for_shutdown(
        sessions=SESSIONS,
        flatten_times=FLATS,
        now=datetime(2026, 11, 2, 22, 50),
        has_position=False,
    )
    assert ok is False
