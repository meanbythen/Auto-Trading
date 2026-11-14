"""
上一交易日成交密集区。

功能：在上一交易日的 K 线里，找出被最多根反复摸到、宽度不超过设定点数的窄价格带。
     观察窗口走完后，收盘在这根带子上方做多，下方做空；还在带子里就先等。
原理：每一根的最高到最低都算「摸到」这些价格。把摸得最勤的一段收成密集区。
     交易日按期货习惯：夜盘 21:00 起算下一天，日盘算当天。
"""

from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from src.data.periods import last_ohlc_price
from src.strategy.daily_body import _naive

from .direction import DIRECTION_DAY_CLUSTER, DIRECTION_LABELS, DirectionContext, DirectionPick


def _int_param(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        out = int(float(value))
    except (TypeError, ValueError):
        return int(default)
    return max(int(lo), min(int(hi), out))


def trading_day_of(ts: datetime | pd.Timestamp) -> date:
    """
    这根 K 线算哪一个交易日。

    晚上 21 点开的夜盘，算第二天的交易日。凌晨收到 8 点前也算这一天。
    """
    stamp = pd.Timestamp(_naive(ts))
    clock = stamp.time()
    day = stamp.date()
    if clock >= dtime(20, 45):
        return day + timedelta(days=1)
    return day


def previous_trading_day_bars(bars: pd.DataFrame, now: datetime) -> pd.DataFrame | None:
    """拿出「现在」之前最近一个已经走完的交易日全部 K 线。"""
    if bars is None or bars.empty:
        return None
    frame = bars.copy()
    if not isinstance(frame.index, pd.DatetimeIndex):
        return None
    moment = pd.Timestamp(_naive(now))
    frame = frame.loc[frame.index < moment]
    if frame.empty:
        return None
    days = pd.Series([trading_day_of(ts) for ts in frame.index], index=frame.index)
    current = trading_day_of(moment)
    earlier = days[days < current]
    if earlier.empty:
        return None
    prev = earlier.max()
    out = frame.loc[days == prev]
    return None if out is None or out.empty else out


def find_touch_cluster(
    day: pd.DataFrame,
    *,
    tick: float = 1.0,
    max_width: float = 10.0,
) -> tuple[float, float, int] | None:
    """
    上一交易日被摸得最多的窄价格带。

    返回 (最低, 最高, 摸到的根数合计)。找不到就返回 None。
    """
    if day is None or day.empty:
        return None
    step = float(tick) if tick and float(tick) > 1e-12 else 1.0
    width = max(step, float(max_width))
    try:
        lows = pd.to_numeric(day["low"], errors="coerce")
        highs = pd.to_numeric(day["high"], errors="coerce")
    except Exception:
        return None
    pair = pd.concat([lows, highs], axis=1).dropna()
    if pair.empty:
        return None
    lo = float(pair.iloc[:, 0].min())
    hi = float(pair.iloc[:, 1].max())
    if hi < lo:
        return None
    start = math_floor_tick(lo, step)
    stop = math_ceil_tick(hi, step)
    levels = np.arange(start, stop + step * 0.5, step)
    if len(levels) < 1:
        return None
    low_a = pair.iloc[:, 0].to_numpy(dtype=float)
    high_a = pair.iloc[:, 1].to_numpy(dtype=float)
    # 每根高低价覆盖到的价位，都记一次「触及」。
    counts = ((low_a[:, None] <= levels) & (high_a[:, None] >= levels)).sum(axis=0)
    max_bins = max(1, int(round(width / step)) + 1)
    best: tuple[float, int, float, float] | None = None
    for size in range(1, min(max_bins, len(levels)) + 1):
        band = (size - 1) * step
        if band > width + 1e-9:
            continue
        for i in range(0, len(levels) - size + 1):
            total = int(counts[i : i + size].sum())
            if total <= 0:
                continue
            avg = total / float(size)
            z_lo = float(levels[i])
            z_hi = float(levels[i + size - 1])
            cand = (avg, total, z_lo, z_hi)
            if best is None or cand[0] > best[0] + 1e-12 or (
                abs(cand[0] - best[0]) <= 1e-12 and cand[1] > best[1]
            ):
                best = cand
    if best is None:
        return None
    return best[2], best[3], best[1]


def math_floor_tick(price: float, tick: float) -> float:
    """往下收到整跳。"""
    return math_round_tick(price, tick, down=True)


def math_ceil_tick(price: float, tick: float) -> float:
    """往上收到整跳。"""
    return math_round_tick(price, tick, down=False)


def math_round_tick(price: float, tick: float, *, down: bool) -> float:
    step = float(tick) if tick and float(tick) > 1e-12 else 1.0
    units = float(price) / step
    snapped = np.floor(units + 1e-9) if down else np.ceil(units - 1e-9)
    return float(snapped * step)


class DayClusterDirection:
    """
    观察窗口走完后，看收盘在上一交易日密集区的上面还是下面。

    还在带子里不算方向，先等它走出去。
    """

    key = DIRECTION_DAY_CLUSTER
    label = DIRECTION_LABELS[DIRECTION_DAY_CLUSTER]
    needs_opening_box = False
    needs_eth_wait = True
    uses_trend_gate = False

    def __init__(self) -> None:
        self.max_width = 10.0

    def configure(self, params: dict[str, Any] | None = None) -> None:
        """密集区最宽多少点。出厂 10，也就是最高减最低不超过 10 点。"""
        box = params or {}
        self.max_width = float(_int_param(box.get("cluster_max_width"), 10, 1, 80))

    def pick(self, ctx: DirectionContext) -> DirectionPick:
        """外面已经等完观察窗口。这里只比收盘和昨天的密集区。"""
        day = previous_trading_day_bars(ctx.bars, ctx.now)
        if day is None or day.empty:
            return DirectionPick(
                "none",
                "还没有上一交易日的K线，画不出成交密集区，先等",
                meta={"direction": self.key},
            )
        tick = 1.0
        host = ctx.host
        if host is not None:
            try:
                tick = float(getattr(host, "price_tick", 1.0) or 1.0)
            except (TypeError, ValueError):
                tick = 1.0
        if tick <= 1e-12:
            tick = 1.0
        found = find_touch_cluster(day, tick=tick, max_width=self.max_width)
        if found is None:
            return DirectionPick(
                "none",
                "上一交易日摸不出窄的成交密集区，先等",
                meta={"direction": self.key},
            )
        z_lo, z_hi, hits = found
        close = last_ohlc_price(ctx.bars, "close")
        if close is None:
            return DirectionPick("none", "没有可用的收盘价，先等", meta={"direction": self.key})
        zone = f"上一交易日密集区 {z_lo:.0f}～{z_hi:.0f}（宽 {z_hi - z_lo:.0f} 点，触及合计 {hits} 次）"
        if close > z_hi + 1e-12:
            return DirectionPick(
                "long",
                f"观察结束，收盘 {close:.2f} 在{zone}上方，做多",
                meta={"direction": self.key, "cluster_low": z_lo, "cluster_high": z_hi},
            )
        if close < z_lo - 1e-12:
            return DirectionPick(
                "short",
                f"观察结束，收盘 {close:.2f} 在{zone}下方，做空",
                meta={"direction": self.key, "cluster_low": z_lo, "cluster_high": z_hi},
            )
        return DirectionPick(
            "none",
            f"观察结束，收盘 {close:.2f} 还在{zone}里面，先等它走出这个区间",
            meta={"direction": self.key, "cluster_low": z_lo, "cluster_high": z_hi},
        )
