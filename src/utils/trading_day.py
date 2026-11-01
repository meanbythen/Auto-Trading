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


def futures_trading_day(moment: datetime | date | str | None = None) -> date:
    """
    现在属于哪个期货交易日。

    算法：时刻在 21:00 及以后，就记到次日；在 21:00 之前（含凌晨、日盘），记到当天。
    所以周一 21:00 到周二 20:59 都算周二这个交易日，周二 21:00 才开始算周三。
    不传参数时看当前时间。传日期字符串（如 "2026-11-01" 或 "2026-11-01 21:30:00"）时按那个时刻算。
    周末顺延到周一：周五 21:00 之后和整个周末，都算下周一。
    """
    if moment is None:
        moment = datetime.now()
    if isinstance(moment, str):
        text = moment.strip()
        parsed: datetime | None = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            return date.fromisoformat(text[:10])
        moment = parsed
    if isinstance(moment, datetime):
        day = moment.date()
        if moment.time() >= NIGHT_START:
            day += timedelta(days=1)
    else:
        day = moment
    # 周六、周日没有交易，统一算到周一。
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day
