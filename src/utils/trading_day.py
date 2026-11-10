"""
期货交易日
功能：按期货实盘的口径算「今天属于哪个交易日」，供换日清零和安全重启使用。
原理：夜盘 21:00 起算下一交易日；从 21:00 一直到次日 20:59（含次日日盘）都归这一日，
以次日日盘日期为准。02:30 只是夜盘收盘，之后到次日 21:00 之前仍算同一交易日。
周末没有交易，落到周末的时间一律顺延到周一。
"""

from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta

# 夜盘开始时间。到了这个点，就已经是下一个交易日了。
NIGHT_START = dtime(21, 0)
# 结算单缓存单独提前一小时换日：程序 20:50 就登录夜盘，
# 若仍按 21:00 换日，这次登录会和上午共用额度，夜盘弹出的结算单会被漏掉。
SETTLEMENT_SESSION_START = dtime(20, 0)


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
