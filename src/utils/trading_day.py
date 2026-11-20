"""
期货交易日
功能：按期货实盘的口径算「今天属于哪个交易日」，供换日清零和安全重启使用；
     并提供交易所休市日历，供收盘清仓时钟跳过节假日和临时休市。
原理：夜盘 21:00 起算下一交易日；从 21:00 一直到次日 20:59（含次日日盘）都归这一日，
以次日日盘日期为准。02:30 只是夜盘收盘，之后到次日 21:00 之前仍算同一交易日。
周末没有交易，落到周末的时间一律顺延到周一。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, time as dtime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.utils.holidays import HolidayCalendar

# 夜盘开始时间。到了这个点，就已经是下一个交易日了。
NIGHT_START = dtime(21, 0)
# 结算单缓存单独提前一小时换日：程序 20:50 就登录夜盘，
# 若仍按 21:00 换日，这次登录会和上午共用额度，夜盘弹出的结算单会被漏掉。
SETTLEMENT_SESSION_START = dtime(20, 0)

# 交易所休市的工作日（只列工作日，周末本来就不交易）。
# 每年国务院公布次年放假安排后更新；临时休市/调休写 config 的 schedule.closed_days。
EXCHANGE_HOLIDAYS: frozenset[date] = frozenset(
    {
        # 2026 年
        date(2026, 1, 1),  # 元旦
        date(2026, 2, 16),  # 春节
        date(2026, 2, 17),
        date(2026, 2, 18),
        date(2026, 2, 19),
        date(2026, 2, 20),
        date(2026, 4, 6),  # 清明
        date(2026, 5, 1),  # 劳动节
        date(2026, 5, 4),
        date(2026, 5, 5),
        date(2026, 6, 19),  # 端午
        date(2026, 9, 25),  # 中秋
        date(2026, 10, 1),  # 国庆
        date(2026, 10, 2),
        date(2026, 10, 5),
        date(2026, 10, 6),
        date(2026, 10, 7),
    }
)


def parse_closed_days(raw: Iterable[object] | None) -> set[date]:
    """把配置里的额外休市日（"2026-12-31"）收成日期集合；坏数据跳过。"""
    out: set[date] = set()
    for item in raw or []:
        if isinstance(item, date) and not isinstance(item, datetime):
            out.add(item)
            continue
        text = str(item or "").strip()
        if not text:
            continue
        try:
            out.add(date.fromisoformat(text[:10]))
        except ValueError:
            continue
    return out


def is_trading_day(
    day: date,
    *,
    extra_closed: Iterable[date] | None = None,
    calendar: HolidayCalendar | None = None,
) -> bool:
    """
    这一天交易所开不开市：周末、节假日、配置里的临时休市都不开。

    传了 calendar 就只看外置日历；没传才用内置的 2026 表，
    这样没配 config/holidays.yaml 的环境行为和以前完全一致。
    """
    if day.weekday() >= 5:
        return False
    if calendar is not None:
        if calendar.is_closed(day):
            return False
    elif day in EXCHANGE_HOLIDAYS:
        return False
    return day not in set(extra_closed or ())


def calendar_coverage_ok(
    calendar: HolidayCalendar | None,
    now: datetime | date | None = None,
    *,
    min_days: int = 60,
) -> bool:
    """
    节假日表是否覆盖到「未来 min_days 天」。

    没配置外置日历时按内置表处理并一律放行：这是旧环境的默认路径，不能因为
    新增检查就突然禁止开仓。配置了但缺年份，实盘就该停开仓。
    """
    if calendar is None:
        return True
    moment = now or datetime.now()
    day = moment.date() if isinstance(moment, datetime) else moment
    horizon = day + timedelta(days=max(int(min_days), 0))
    return calendar.covers(horizon)


def _parse_moment(moment: datetime | date | str | None) -> datetime | date:
    """把字符串、日期收成 datetime 或 date，给下面两套换日规则共用。"""
    if moment is None:
        return datetime.now()
    if isinstance(moment, str):
        text = moment.strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
        return date.fromisoformat(text[:10])
    return moment


def _session_day(moment: datetime | date | str | None, switch: dtime) -> date:
    """到了 switch 就算下一天，周末顺延到周一。"""
    stamp = _parse_moment(moment)
    if isinstance(stamp, datetime):
        day = stamp.date()
        if stamp.time() >= switch:
            day += timedelta(days=1)
    else:
        day = stamp
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def futures_trading_day(moment: datetime | date | str | None = None) -> date:
    """
    现在属于哪个期货交易日。

    算法：时刻在 21:00 及以后，就记到次日；在 21:00 之前（含凌晨、日盘），记到当天。
    所以周一 21:00 到周二 20:59 都算周二这个交易日，周二 21:00 才开始算周三。
    不传参数时看当前时间。传日期字符串（如 "2026-11-01" 或 "2026-11-01 21:30:00"）时按那个时刻算。
    周末顺延到周一：周五 21:00 之后和整个周末，都算下周一。
    """
    return _session_day(moment, NIGHT_START)


def next_trading_day(
    moment: datetime | date | str | None = None,
    *,
    extra_closed: Iterable[date] | None = None,
    calendar: HolidayCalendar | None = None,
) -> date:
    """
    moment 所属交易日的下一个交易日，跳过周末和休市日。

    回测结束日只算到当天 20:59，当晚 21:00 的夜盘属于下一交易日。
    勾「含结束日当晚夜盘」时用它把结束日往后推一格。
    """
    day = futures_trading_day(moment) + timedelta(days=1)
    while not is_trading_day(day, extra_closed=extra_closed, calendar=calendar):
        day += timedelta(days=1)
    return day


def trading_days_between(
    start: datetime | date | str | None,
    end: datetime | date | str | None,
) -> int:
    """
    从 start 所属交易日到 end 所属交易日，中间过了几个交易日。

    同一交易日是 0。周五到下周一是 1（周末没有盘）。
    周五夜盘本身已经算下周一，所以周五夜盘和周一白天仍是 0。
    只跳过周末，节假日按普通工作日算，和本文件其它规则一致。
    """
    first = futures_trading_day(start)
    last = futures_trading_day(end)
    if last <= first:
        return 0
    passed = 0
    cursor = first
    while cursor < last:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            passed += 1
    return passed


def settlement_session_day(moment: datetime | date | str | None = None) -> date:
    """
    结算单缓存用的「会话日」。

    和期货交易日同一套周末规则，但 20:00 起就算下一天。
    这样每天 20:50 的夜盘登录会重新查一次结算单，次日日盘沿用这一次。
    盈亏清零仍用 futures_trading_day，不受这里影响。
    """
    return _session_day(moment, SETTLEMENT_SESSION_START)
