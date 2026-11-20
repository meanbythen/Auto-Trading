"""交易日内的标准K线时间。

回测、数据覆盖检查和研究数据集共用这一份，避免数据层反过来依赖回测引擎。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from src.data.periods import session_close_times
from src.utils.trading_day import EXCHANGE_HOLIDAYS

if TYPE_CHECKING:
    from src.utils.holidays import HolidayCalendar

_CLOSED_WEEKDAYS = EXCHANGE_HOLIDAYS


def _closed_day(day: date, calendar: HolidayCalendar | None = None) -> bool:
    """这一天是不是休市。传了外置日历时只认它，否则用内置 2026 表。"""
    if calendar is not None:
        return calendar.is_closed(day)
    return day in _CLOSED_WEEKDAYS


def session_bar_times(
    day: date,
    step_minutes: int,
    *,
    include_night: bool = True,
    calendar: HolidayCalendar | None = None,
) -> list[datetime]:
    """
    这一天按交易时段应该有的K线收盘时间。

    只生成连续交易的那些分钟。午休和 10:15 之后的休息不生成，所以正常休盘不会被当成缺口。
    """
    if day.weekday() >= 5 or _closed_day(day, calendar):
        return []

    # 第二天休市时，前一天晚上通常没有夜盘。没有夜盘不算数据断开。
    night_ok = include_night and not _closed_day(day + timedelta(days=1), calendar)
    return session_close_times(day, step_minutes, include_night=night_ok)
