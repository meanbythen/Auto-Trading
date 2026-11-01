"""
复盘用的交易时段
功能：把「某一天」拆成日盘、夜盘两段，并判断这段有没有走完。
原理：
  - 日盘固定看当天 09:00–15:00 的K线（含 15:00 这根）。
  - 夜盘属于这个交易日：看上一交易日 21:00 到当天 02:30，这样沥青这种 23:00 收的、
    也覆盖少数收得更晚的品种。周一的夜盘从上周五 21:00 起。
  - 复盘可以只看其中一段，也可以两段一起看（前夜盘+日盘）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta


# 界面上三个选项，必须和这里的字完全一致
SCOPE_DAY = "日盘"
SCOPE_NIGHT = "夜盘"
SCOPE_BOTH = "前夜盘+日盘"
SCOPES = (SCOPE_DAY, SCOPE_NIGHT, SCOPE_BOTH)

_DAY_START = time(9, 0)
_DAY_END = time(15, 0)
_NIGHT_START = time(21, 0)
_NIGHT_END = time(2, 30)


@dataclass
class SessionWindow:
    """一段要回顾的时间。start/end 都是本地时间，含两端。"""

    name: str
    start: datetime
    end: datetime

    def contains(self, ts: datetime) -> bool:
        """这根K线或这条日志是否落在本段里。"""
        return self.start <= ts <= self.end


def previous_trading_day(day: date) -> date:
    """前一个交易日。周六、周日没有交易，向前落到周五。"""
    prev = day - timedelta(days=1)
    while prev.weekday() >= 5:
        prev -= timedelta(days=1)
    return prev


def windows_for(trade_date: date, scope: str) -> list[SessionWindow]:
    """
    按用户选的范围生成时间段。

    参数:
        trade_date: 用户想复盘的那个交易日（日盘那一天）
        scope: 日盘 / 夜盘 / 前夜盘+日盘
    """
    scope = (scope or SCOPE_BOTH).strip()
    if scope not in SCOPES:
        scope = SCOPE_BOTH
    out: list[SessionWindow] = []
    if scope in (SCOPE_NIGHT, SCOPE_BOTH):
        # 夜盘是上一交易日晚上开的，跨过零点到这里收盘。周一时从前一周五晚上算起。
        night_start = previous_trading_day(trade_date)
        out.append(
            SessionWindow(
                name="夜盘",
                start=datetime.combine(night_start, _NIGHT_START),
                end=datetime.combine(trade_date, _NIGHT_END),
            )
        )
    if scope in (SCOPE_DAY, SCOPE_BOTH):
        out.append(
            SessionWindow(
                name="日盘",
                start=datetime.combine(trade_date, _DAY_START),
                end=datetime.combine(trade_date, _DAY_END),
            )
        )
    return out


def closure_lines(windows: list[SessionWindow], now: datetime | None = None) -> list[str]:
    """
    告诉用户这段盘走完没有。
    还没收盘时，后面的分析只能覆盖已经出现的K线，结论是临时的。
    """
    now = now or datetime.now()
    lines: list[str] = []
    for w in windows:
        if now >= w.end:
            lines.append(f"{w.name}已收盘（{w.start:%H:%M}–{w.end:%m-%d %H:%M}）。")
        elif now < w.start:
            lines.append(f"{w.name}还没开始（{w.start:%m-%d %H:%M} 才开盘）。")
        else:
            lines.append(
                f"{w.name}还在走（现在 {now:%H:%M}，要到 {w.end:%m-%d %H:%M} 才收）。"
                "下面只能看到已经走出来的部分，收盘后再看一次更完整。"
            )
    return lines


def any_still_open(windows: list[SessionWindow], now: datetime | None = None) -> bool:
    """只要有一段还在交易中，综合结论就要标明「临时」。"""
    now = now or datetime.now()
    return any(w.start <= now < w.end for w in windows)
