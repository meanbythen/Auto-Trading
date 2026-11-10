"""
Jesse 价值区（VA）近似计算。

功能：用分钟 K 线的高低价 + 成交量，估一个时段里 70% 成交量所在的价格区间。
原理：CTP 没有逐笔足迹，不能做 ATAS 那种真实 Market Profile。
     这里按资料《VA 自动计算》把一根 K 的成交量，均匀摊到从最低到最高的每一跳，
     再从成交量最大的那一跳（VPOC）向两边扩，直到攒够总成交量的 70%。
     这是近似，用来区分「还在价值区里晃」和「已经离开价值区」。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

import pandas as pd

from src.utils.trading_day import futures_trading_day


@dataclass(frozen=True)
class ValueArea:
    """一个时段算出来的价值区。"""

    vah: float  # 价值区上沿
    val: float  # 价值区下沿
    vpoc: float  # 成交量最大的价格
    session_key: str


def session_key(
    moment: datetime | pd.Timestamp | None,
    sessions: list[list[str]] | None = None,
) -> str | None:
    """
    把一根 K 线归到「哪个交易日的日盘/夜盘」。

    有品种表时段时按表判断（燃油夜盘到 23:00，白银到 02:30）。
    没表时退回：夜盘 21:00 到次日 02:59，日盘 09:00 到 14:59。
    中午休市、下午收盘到夜盘开盘之间的时间不算进任何时段。
    夜盘开盘起算下一个期货交易日。
    """
    if moment is None:
        return None
    stamp = pd.Timestamp(moment).to_pydatetime()
    if sessions:
        from src.utils.session_clock import in_clock_window, parse_hhmm, split_day_night_sessions

        day_rows, night_rows = split_day_night_sessions(sessions)
        kind = None
        for row in night_rows:
            if in_clock_window(parse_hhmm(row[0]), parse_hhmm(row[1]), stamp, grace_sec=0):
                kind = "night"
                break
        if kind is None:
            for row in day_rows:
                if in_clock_window(parse_hhmm(row[0]), parse_hhmm(row[1]), stamp, grace_sec=0):
                    kind = "day"
                    break
        if kind is None:
            return None
    else:
        hour = stamp.hour
        if hour >= 21 or hour < 3:
            kind = "night"
        elif 9 <= hour < 15:
            kind = "day"
        else:
            return None
    day = futures_trading_day(stamp)
    return f"{day.isoformat()}_{kind}"


def calc_va_from_rows(
    rows: Iterable[Any],
    tick: float = 1.0,
    value_pct: float = 0.70,
) -> tuple[float, float, float] | None:
    """
    输入一个时段的 K 线，返回 (价值区上沿, 价值区下沿, VPOC)。
    根数太少或没有成交量时返回 None。
    """
    tick = float(tick) if tick and float(tick) > 0 else 1.0
    vol_map: dict[float, float] = defaultdict(float)
    total = 0.0
    count = 0
    for row in rows:
        if isinstance(row, dict):
            high = float(row["high"])
            low = float(row["low"])
            volume = float(row.get("volume") or 0.0)
        elif isinstance(row, pd.Series):
            high = float(row["high"])
            low = float(row["low"])
            try:
                volume = float(row["volume"])
            except (KeyError, TypeError, ValueError):
                volume = 0.0
        else:
            high = float(row.high)
            low = float(row.low)
            try:
                volume = float(row.volume)
            except (AttributeError, TypeError, ValueError):
                volume = 0.0
        if volume <= 0:
            continue
        count += 1
        # 高低价之间有几跳，就把成交量均摊到这些跳上。
        steps = max(1, int(round((high - low) / tick)))
        if steps <= 1:
            price = round(low / tick) * tick
            vol_map[price] += volume
            total += volume
            continue
        piece = volume / (steps + 1)
        for i in range(steps + 1):
            price = round((low + i * tick) / tick) * tick
            vol_map[price] += piece
        total += volume
    if count < 10 or total <= 0 or not vol_map:
        return None
    prices = sorted(vol_map)
    vpoc = max(vol_map.items(), key=lambda item: item[1])[0]
    target = total * float(value_pct)
    acc = vol_map[vpoc]
    idx = prices.index(vpoc)
    left = idx
    right = idx
    # 哪边成交量更大，就先往哪边扩，直到攒够 70%。
    while acc < target and (left > 0 or right < len(prices) - 1):
        left_vol = vol_map[prices[left - 1]] if left > 0 else -1.0
        right_vol = vol_map[prices[right + 1]] if right < len(prices) - 1 else -1.0
        if left_vol >= right_vol and left > 0:
            left -= 1
            acc += left_vol
        elif right < len(prices) - 1:
            right += 1
            acc += right_vol
        else:
            break
    return float(prices[right]), float(prices[left]), float(vpoc)


def previous_session_va(
    bars: pd.DataFrame,
    now: datetime | pd.Timestamp | None = None,
    tick: float = 1.0,
    value_pct: float = 0.70,
    sessions: list[list[str]] | None = None,
) -> ValueArea | None:
    """
    用「上一完整日盘或夜盘」算价值区。

    ATAS 模板写的是 Previous Session。当前时段自己还在走，VA 会一直变，
    所以这里固定用已经走完的上一个时段，避免开盘前几根把区间算得太窄。
    """
    if bars is None or bars.empty:
        return None
    grouped: dict[str, list[pd.Series]] = {}
    order: list[str] = []
    for stamp, row in bars.iterrows():
        key = session_key(stamp, sessions)
        if key is None:
            continue
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(row)
    if now is not None:
        current = session_key(now, sessions)
    else:
        current = session_key(bars.index[-1], sessions)
    prev_key = None
    for key in reversed(order):
        if current is None or key != current:
            prev_key = key
            break
    if prev_key is None:
        return None
    result = calc_va_from_rows(grouped[prev_key], tick=tick, value_pct=value_pct)
    if result is None:
        return None
    vah, val, vpoc = result
    return ValueArea(vah=vah, val=val, vpoc=vpoc, session_key=prev_key)


def is_imbalance(close: float, area: ValueArea | None) -> bool:
    """收盘价离开价值区上沿或下沿，才算失衡，才允许做趋势单。"""
    if area is None:
        return True
    return close > area.vah or close < area.val


def resample_minutes(bars: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """把更细的 K 线合成更大周期，用来算 15 分钟方向偏见。"""
    if bars is None or bars.empty:
        return bars
    minutes = max(1, int(minutes))
    frame = bars.copy()
    if not isinstance(frame.index, pd.DatetimeIndex):
        frame.index = pd.to_datetime(frame.index)
    agg: dict[str, str] = {"open": "first", "high": "max", "low": "min", "close": "last"}
    if "volume" in frame.columns:
        agg["volume"] = "sum"
    out = frame.resample(f"{minutes}min", label="left", closed="left").agg(agg)
    return out.dropna(subset=["open", "high", "low", "close"])
