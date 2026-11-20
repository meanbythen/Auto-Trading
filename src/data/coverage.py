"""
K 线时段覆盖。

功能：按「开始时间 + 结束时间 + 周期」算出这段时间应该有多少根、缺了哪几段，
并识别这次读图实际读到的周期和所选周期是否一致。
原理：交易日和交易时段沿用回测的 session_bar_times，休市不生成时间点，
所以午休、10:15 小休、隔夜和周末不会被当成缺口。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

import pandas as pd

from src.data.periods import canonical_period, period_label


class PeriodMismatchError(RuntimeError):
    """读图读到的周期和界面所选不一致。"""

    def __init__(self, expected: str, detected: str | None) -> None:
        self.expected = expected
        self.detected = detected
        found = period_label(detected) if detected else "认不出"
        super().__init__(
            f"读到的K线是「{found}」，和选的「{period_label(expected)}」不一致。"
            "请先在期货通主图把周期点成一致，再读一次。这次没有存库。"
        )


@dataclass(frozen=True, slots=True)
class CoverageReport:
    """请求时段内的覆盖情况。"""

    expected_n: int
    have_n: int
    missing_n: int
    missing_ranges: tuple[tuple[str, str, int], ...]
    first_ts: str
    last_ts: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "expected_n": self.expected_n,
            "have_n": self.have_n,
            "missing_n": self.missing_n,
            "missing_ranges": [list(item) for item in self.missing_ranges],
            "first_ts": self.first_ts,
            "last_ts": self.last_ts,
        }


def _naive(value: datetime | pd.Timestamp) -> datetime:
    stamp = pd.Timestamp(value).to_pydatetime()
    if stamp.tzinfo is not None:
        stamp = stamp.replace(tzinfo=None)
    return stamp.replace(second=0, microsecond=0)


def _is_trading_day(day: date) -> bool:
    """这一天有没有交易时段。休市日返回 False。"""
    from src.data.sessions import session_bar_times

    return bool(session_bar_times(day, 60))


def _session_times(day: date, step: int) -> list[datetime]:
    from src.data.sessions import session_bar_times

    return session_bar_times(day, step)


def expected_bar_times(
    start: datetime | pd.Timestamp,
    end: datetime | pd.Timestamp,
    period: str,
) -> pd.DatetimeIndex:
    """
    这段时间按交易时段应该出现的 K 线结束时刻。

    分钟线按 session_bar_times 逐日生成；日线每个交易日 15:00 一根；
    周线每周五 15:00 一根。开始和结束都按分钟边界算，两端都包含。
    """
    first = _naive(start)
    last = _naive(end)
    if first > last:
        return pd.DatetimeIndex([])
    code = canonical_period(period)
    stamps: list[datetime] = []
    day = first.date()
    while day <= last.date():
        if _is_trading_day(day):
            if code == "W":
                if day.weekday() == 4:
                    stamps.append(datetime.combine(day, time(15, 0)))
            elif code == "D":
                # 程序里日线时间戳是交易日 00:00，不是 15:00。
                stamps.append(datetime.combine(day, time(0, 0)))
            else:
                for stamp in _session_times(day, max(1, int(code))):
                    if first <= stamp <= last:
                        stamps.append(stamp)
        day += timedelta(days=1)
    if code in ("D", "W"):
        stamps = [stamp for stamp in stamps if first <= stamp <= last]
    return pd.DatetimeIndex(stamps)


def expected_bar_count(
    start: datetime | pd.Timestamp,
    end: datetime | pd.Timestamp,
    period: str,
) -> int:
    """这段时间应该有多少根。"""
    return len(expected_bar_times(start, end, period))


def coverage_report(
    bars: pd.DataFrame | None,
    start: datetime | pd.Timestamp,
    end: datetime | pd.Timestamp,
    period: str,
    *,
    max_ranges: int | None = 5,
) -> CoverageReport:
    """
    算请求时段内已经存了多少根、还缺多少根。

    bars 可以是整库（时段外的不算）。缺口按预期时间顺序连着数，
    跨午休的两段不会被合成一段。
    """
    expected = expected_bar_times(start, end, period)
    present: set[pd.Timestamp] = set()
    if bars is not None and len(bars) > 0:
        index = pd.DatetimeIndex(pd.to_datetime(bars.index)).floor("s")
        present = set(index)
    have = [stamp for stamp in expected if stamp in present]
    missing = [stamp for stamp in expected if stamp not in present]
    ranges: list[tuple[str, str, int]] = []
    group_start: pd.Timestamp | None = None
    prev: pd.Timestamp | None = None
    prev_pos = -10
    count = 0
    for pos, stamp in enumerate(expected):
        if stamp in present:
            continue
        # 只有预期序列里紧挨着的两根才并成一段；跨午休、跨日自然断开。
        if group_start is None:
            group_start = stamp
            count = 1
        elif pos == prev_pos + 1:
            count += 1
        else:
            ranges.append((_fmt(group_start), _fmt(prev), count))
            group_start = stamp
            count = 1
        prev = stamp
        prev_pos = pos
    if group_start is not None and prev is not None:
        ranges.append((_fmt(group_start), _fmt(prev), count))
    return CoverageReport(
        expected_n=len(expected),
        have_n=len(have),
        missing_n=len(missing),
        missing_ranges=tuple(ranges if max_ranges is None else ranges[: max(0, int(max_ranges))]),
        first_ts=_fmt(have[0]) if have else "",
        last_ts=_fmt(have[-1]) if have else "",
    )


def _fmt(stamp: pd.Timestamp | datetime) -> str:
    return pd.Timestamp(stamp).strftime("%Y-%m-%d %H:%M")


def _median_minutes(bars: pd.DataFrame) -> float | None:
    if bars is None or len(bars) < 2:
        return None
    index = pd.DatetimeIndex(pd.to_datetime(bars.index)).sort_values()
    diffs = index.to_series().diff().dropna().dt.total_seconds() / 60.0
    if diffs.empty:
        return None
    return float(diffs.median())


def detect_period(bars: pd.DataFrame | None) -> str | None:
    """按相邻 K 线时间差的中位数猜周期：1/5/15/30/60/D/W。"""
    median = _median_minutes(bars) if bars is not None else None
    if median is None or median <= 0:
        return None
    if median >= 60 * 24 * 4:
        return "W"
    if median >= 60 * 20:
        return "D"
    for step in (1, 5, 15, 30, 60):
        if abs(median - step) <= max(0.6, step * 0.2):
            return str(step)
    return str(int(round(median)))


def ensure_period(bars: pd.DataFrame | None, period: str) -> None:
    """读到的周期和所选不一致时抛 PeriodMismatchError。根数少于 2 时不判断。"""
    detected = detect_period(bars) if bars is not None else None
    if detected is None:
        return
    expected = canonical_period(period)
    if expected == "W":
        ok = detected == "W"
    elif expected == "D":
        ok = detected == "D"
    else:
        ok = detected.isdigit() and int(detected) == int(expected)
    if not ok:
        raise PeriodMismatchError(expected, detected)


def require_timestamps(bars: pd.DataFrame | None) -> None:
    """
    每根 K 线都必须有时间戳，且不能重复。

    同花顺信息框顶部读不到时间时，这一根不能落库——宁可不存，
    也不要让一堆没有时间的 K 线混进本地库。
    """
    if bars is None or len(bars) == 0:
        raise ValueError("这次没有读到 K 线，没有可存库的数据。")
    index = pd.DatetimeIndex(pd.to_datetime(bars.index, errors="coerce"))
    if index.isna().any():
        raise ValueError("读到的 K 线里有时间戳缺失的根，已停止存库。请重读一次。")
    if index.duplicated().any():
        raise ValueError("读到的 K 线里有重复时间戳，已停止存库。请重读一次。")
