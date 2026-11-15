"""期货交易日：夜盘 21:00 起算下一个交易日，周末顺延到周一。"""

from datetime import date, datetime

from src.utils.holidays import HolidayCalendar
from src.utils.trading_day import (
    futures_trading_day,
    is_trading_day,
    settlement_session_day,
    trading_days_between,
)


def test_day_session_belongs_to_the_same_day() -> None:
    """日盘 09:00 还是当天。"""
    assert futures_trading_day(datetime(2026, 11, 2, 9, 0)) == date(2026, 11, 2)


def test_night_session_starts_the_next_trading_day() -> None:
    """21:00 一到就算下一个交易日，不必等零点。"""
    assert futures_trading_day(datetime(2026, 11, 2, 21, 30)) == date(2026, 11, 3)


def test_after_midnight_night_session_stays_on_its_trading_day() -> None:
    """周二凌晨 00:30 是周一晚上开的夜盘，仍算周二这个交易日。"""
    assert futures_trading_day(datetime(2026, 11, 3, 0, 30)) == date(2026, 11, 3)


def test_just_before_night_start_is_still_the_old_day() -> None:
    """20:59 还没到夜盘。"""
    assert futures_trading_day(datetime(2026, 10, 30, 20, 59)) == date(2026, 10, 30)


def test_friday_night_and_the_weekend_both_count_as_monday() -> None:
    """周五夜盘和周六、周日没有下一个交易时段，都算下周一。"""
    assert futures_trading_day(datetime(2026, 10, 30, 21, 30)) == date(2026, 11, 2)
    assert futures_trading_day(datetime(2026, 10, 31, 10, 0)) == date(2026, 11, 2)
    assert futures_trading_day(datetime(2026, 11, 1, 22, 0)) == date(2026, 11, 2)


def test_accepts_date_strings() -> None:
    """状态文件里存的是字符串，读回来也要能算出交易日。"""
    assert futures_trading_day("2026-11-01 21:30:00") == date(2026, 11, 2)
    assert futures_trading_day("2026-11-02") == date(2026, 11, 2)


def test_settlement_session_flips_before_night_login() -> None:
    """20:50 夜盘登录必须已经算下一天，不能和上午 08:50 共用结算单额度。"""
    assert settlement_session_day(datetime(2026, 11, 2, 8, 50)) == date(2026, 11, 2)
    assert settlement_session_day(datetime(2026, 11, 2, 19, 59)) == date(2026, 11, 2)
    assert settlement_session_day(datetime(2026, 11, 2, 20, 0)) == date(2026, 11, 3)
    assert settlement_session_day(datetime(2026, 11, 2, 20, 50)) == date(2026, 11, 3)
    # 盈亏换日仍是 21:00，两套规则不要混。
    assert futures_trading_day(datetime(2026, 11, 2, 20, 50)) == date(2026, 11, 2)
    assert settlement_session_day(datetime(2026, 10, 30, 20, 50)) == date(2026, 11, 2)


def test_calendar_overrides_builtin_table() -> None:
    """传了外置日历就只看它：2027 的节假日内置表不认识，日历认识。"""
    calendar = HolidayCalendar({2027: frozenset({date(2027, 10, 1)})}, source="test")
    assert not is_trading_day(date(2027, 10, 1), calendar=calendar)
    assert is_trading_day(date(2027, 10, 1))
    # 日历只配了 2027：2026 没配置的年份按「不休息」处理，缺覆盖由启动检查拦开仓。
    assert is_trading_day(date(2026, 1, 1), calendar=calendar)


def test_trading_days_between_same_session_is_zero() -> None:
    """夜盘和次日日盘是同一个交易日，中间过了 0 天。"""
    assert trading_days_between(datetime(2026, 4, 21, 22, 30), datetime(2026, 4, 22, 10, 0)) == 0
    assert trading_days_between(datetime(2026, 4, 21, 22, 30), datetime(2026, 4, 23, 9, 0)) == 1
    assert trading_days_between(datetime(2026, 10, 30, 22, 30), datetime(2026, 11, 2, 10, 0)) == 0
    assert trading_days_between(datetime(2026, 10, 30, 22, 30), datetime(2026, 11, 3, 9, 0)) == 1
